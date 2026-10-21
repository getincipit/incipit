"""What a merge does with the work already done on the documents it joins.

The transcription is covered by test_merge.py. This is about everything a reviewer put *around*
it: their verdict, the comments they left, and the history of their edits.
"""
import pytest
from conftest import tiny_image_bytes, seed_draft

from incipit import accounts, collab, db, merge, split
from incipit.config import Settings

# Confirmed documents take their display ids as internal ids, so the tests below can name them.
pytestmark = pytest.mark.usefixtures("readable_ids")


def _settings(tmp_path) -> Settings:
    return Settings(
        bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
        db_path=str(tmp_path / "extraction.db"),
        openrouter_api_key="sk-test",
    )


@pytest.fixture
def stub_model(monkeypatch):
    def fake_call(messages, settings, schema=None, extract_result=None):
        return extract_result(
            {"document_type": "Letter", "date": "1889-03-14", "author": "Hayes, Howard W",
             "recipient": "Lovell, Ada Mary", "place": "Newark, N.J.",
             "names": [], "subjects": ["Patents"]},
            {"cost": 0.0002},
        )
    monkeypatch.setattr(merge.openrouter, "call_openrouter", fake_call)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """Two drafted documents, an editor, and a reviewer who has been working on them."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(3)], settings)
    groups = [{"display_id": "E2241AAA", "sequence_indices": [0]},
              {"display_id": "E2241AAB", "sequence_indices": [1, 2]}]
    split.confirm_batch_split(batch_id, groups, settings)
    with db.connect(settings.db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        paul = accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        for group in groups:
            document_id = group["display_id"]
            seed_draft(
                conn, document_id, model="qwen", provider="openrouter",
                result={"text": f"text of {document_id}", "challenges": [], "uncertain_words": [],
                        "marginal_notes": [], "places": [], "names": []},
                ai_metadata={"title": f"{document_id}, Letter", "document_type": "Letter",
                             "date": "1889-01-01", "author": "Lovell, Ada Mary",
                             "recipient": "", "place": "", "subjects": ""},
                ocr_draft_text="ocr", output_json={"document_id": document_id},
                cost_estimate=0.001)
    return settings, paul


def reviewed(settings, document_id, status="accepted", reviewer="Paul"):
    """The admin's verdict, which is what new_documents.review_status holds."""
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = ?, reviewer = ? "
                     "WHERE document_id = ?", (status, reviewer, document_id))


def commented(settings, document_id, author_id, body):
    with db.connect(settings.db_path) as conn:
        collab.add_comment(conn, document_id, "new", author_id, body)


def edited(settings, document_id, author_id, before, after):
    with db.connect(settings.db_path) as conn:
        collab.record_revision(conn, document_id, "new", "reviewed_text", before, after, author_id)


def merged_state(settings, document_id="E2241AAA"):
    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, document_id)
        comments = [c["body"] for c in collab.comments_for(conn, document_id, "new")]
        revisions = collab.revisions_for(conn, document_id, "new")
    return row, comments, revisions


@pytest.mark.parametrize(
    ("first_status", "second_status"),
    [
        pytest.param("accepted", "accepted", id="two-reviewed"),
        pytest.param("accepted", "unreviewed", id="one-reviewed-one-not-reviewed"),
        pytest.param("unreviewed", "unreviewed", id="two-not-reviewed"),
    ],
)
@pytest.mark.parametrize("with_comments", [False, True], ids=["without-comments", "with-comments"])
@pytest.mark.parametrize("with_history", [False, True], ids=["without-history", "with-history"])
def test_merge_preserves_collaboration_across_every_review_state(
        folder, stub_model, first_status, second_status, with_comments, with_history):
    """The review verdict, comments, and edit history are independent dimensions.

    Exercise their complete supported matrix so a future change cannot accidentally make comment
    or history migration depend on whether either source document happened to be reviewed.
    """
    settings, paul = folder
    reviewed(settings, "E2241AAA", first_status,
             "Paul" if first_status == "accepted" else None)
    reviewed(settings, "E2241AAB", second_status,
             "Paul" if second_status == "accepted" else None)
    if with_comments:
        commented(settings, "E2241AAA", paul, "comment from the first document")
        commented(settings, "E2241AAB", paul, "comment from the second document")
    if with_history:
        edited(settings, "E2241AAA", paul, "first draft", "first corrected")
        edited(settings, "E2241AAB", paul, "second draft", "second corrected")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings, author_id=paul)

    row, comments, revisions = merged_state(settings)
    assert row["review_status"] == "unreviewed"
    assert row["reviewer"] is None
    assert sorted(comments) == (
        ["comment from the first document", "comment from the second document"]
        if with_comments else []
    )
    source_edits = [entry for entry in revisions if entry["field"] == "reviewed_text"]
    assert {entry["previous"] for entry in source_edits} == (
        {"first draft", "second draft"} if with_history else set()
    )
    merge_notes = [entry for entry in revisions if entry["field"] == collab.MERGE_NOTE_FIELD]
    assert len(merge_notes) == 1


# ---------- the verdict ----------


def test_merging_a_reviewed_document_with_an_unreviewed_one_leaves_it_unreviewed(
        folder, stub_model):
    """The merged document is not the one anybody accepted: it has different text, different
    metadata and a page nobody has looked at in this context. It has to be reviewed again."""
    settings, _ = folder
    reviewed(settings, "E2241AAA", "accepted")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    row, _, _ = merged_state(settings)
    assert row["review_status"] == "unreviewed"


def test_merging_two_accepted_documents_still_leaves_one_unreviewed_document(folder, stub_model):
    """Both halves being accepted does not make the join accepted."""
    settings, _ = folder
    reviewed(settings, "E2241AAA", "accepted")
    reviewed(settings, "E2241AAB", "accepted")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    row, _, _ = merged_state(settings)
    assert row["review_status"] == "unreviewed"


def test_no_reviewer_verdict_survives_the_merge_either(folder, stub_model):
    """A reviewer's own accept lives in document_reviews, and it is about the old document."""
    settings, paul = folder
    with db.connect(settings.db_path) as conn:
        db.set_document_review(conn, "E2241AAA", "new", "reviewer", "accepted", "John")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        verdicts = conn.execute(
            "SELECT status FROM document_reviews WHERE document_id = 'E2241AAA'").fetchall()
    assert [v["status"] for v in verdicts] == []


# ---------- the comments ----------


def test_comments_from_both_documents_are_on_the_merged_one(folder, stub_model):
    """Asked for: a comment is a question someone still needs answering, and the merge does not
    answer it. Losing it loses the request."""
    settings, paul = folder
    commented(settings, "E2241AAA", paul, "check the date on this one")
    commented(settings, "E2241AAB", paul, "the second half is upside down")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    _, comments, _ = merged_state(settings)
    assert sorted(comments) == ["check the date on this one", "the second half is upside down"]


def test_a_comment_keeps_who_wrote_it(folder, stub_model):
    settings, paul = folder
    commented(settings, "E2241AAB", paul, "the second half is upside down")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        entry = collab.comments_for(conn, "E2241AAA", "new")[0]
    assert entry["by"] == "Paul"


# ---------- the history ----------


def test_the_history_of_both_documents_survives_the_merge(folder, stub_model):
    """Asked for: the edits people made are the record of the work, and a merge is not a reason to
    forget them."""
    settings, paul = folder
    edited(settings, "E2241AAA", paul, "first draft", "first corrected")
    edited(settings, "E2241AAB", paul, "second draft", "second corrected")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    _, _, revisions = merged_state(settings)
    assert {r["previous"] for r in revisions} >= {"first draft", "second draft"}


def test_the_history_says_the_document_was_merged(folder, stub_model):
    """Asked for: without it, a document's history begins mid-story with no explanation of why the
    text it describes is not the text that is there."""
    settings, _ = folder

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    _, _, revisions = merged_state(settings)
    notes = [r for r in revisions if r["field"] == collab.MERGE_NOTE_FIELD]
    assert len(notes) == 1


def test_the_merge_note_names_the_documents_that_went_into_it(folder, stub_model):
    settings, _ = folder

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    _, _, revisions = merged_state(settings)
    note = next(r for r in revisions if r["field"] == collab.MERGE_NOTE_FIELD)
    assert "E2241AAA" in note["current"] and "E2241AAB" in note["current"]


def test_the_merge_note_reads_as_a_sentence_in_the_history_panel(folder, stub_model):
    """It sits in the same list as "Transcription changed by Paul", so it has to say what it is."""
    settings, _ = folder

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    _, _, revisions = merged_state(settings)
    note = next(r for r in revisions if r["field"] == collab.MERGE_NOTE_FIELD)
    assert note["label"] == "Merged"


def test_editing_the_merge_does_not_rewrite_a_source_documents_history(folder, stub_model):
    """A recent source revision is carried into the join, but the merge is a structural boundary:
    autosave coalescing after it must create a new entry rather than extending the source's edit."""
    settings, paul = folder
    edited(settings, "E2241AAA", paul, "source draft", "source correction")
    merge.merge_documents(["E2241AAA", "E2241AAB"], settings, author_id=paul)

    with db.connect(settings.db_path) as conn:
        collab.record_revision(
            conn, "E2241AAA", "new", "reviewed_text",
            "joined transcription", "joined transcription corrected", paul)
        entries = conn.execute(
            "SELECT previous, current FROM text_revisions "
            "WHERE document_id = 'E2241AAA' AND field = 'reviewed_text' ORDER BY id"
        ).fetchall()

    assert [(row["previous"], row["current"]) for row in entries] == [
        ("source draft", "source correction"),
        ("joined transcription", "joined transcription corrected"),
    ]


# ---------- and a database that has no collaboration tables at all ----------


