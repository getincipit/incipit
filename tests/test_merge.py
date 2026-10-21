"""Merging wrongly-split documents back together."""

import json

import pytest
from conftest import bill, document_named, next_document_ids, tiny_image_bytes, seed_draft

from incipit import db, docid, fields, merge, prompting, split
from incipit.config import Settings
from incipit.storage import BucketStorage


def _settings(tmp_path) -> Settings:
    return Settings(
        bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
        db_path=str(tmp_path / "extraction.db"),
        openrouter_api_key="sk-test",
    )


def _drafted_folder(tmp_path, groups=None, pages=6):
    """A folder confirmed into documents and drafted -- the state a merge operates on."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(pages)], settings, folder_display_id="E2241"
    )
    groups = groups or [
        {"display_id": "E2241AAA", "sequence_indices": [0, 1]},
        {"display_id": "E2241AAB", "sequence_indices": [2]},
        {"display_id": "E2241AAC", "sequence_indices": [3, 4, 5]},
    ]
    with next_document_ids(*(g["display_id"] for g in groups)):
        split.confirm_batch_split(batch_id, groups, settings)

    with db.connect(settings.db_path) as conn:
        for group in groups:
            document_id = group["display_id"]
            seed_draft(
                conn,
                document_id,
                model="qwen",
                provider="openrouter",
                result={
                    "text": f"text of {document_id}",
                    "challenges": ["handwriting"],
                    "uncertain_words": [f"word-{document_id}"],
                    "marginal_notes": [],
                    "places": ["Albany, N.Y."],
                    "names": ["Lovell, Ada Mary", f"Clerk, {document_id}"],
                },
                ai_metadata={
                    "title": f"{document_id}, Letter",
                    "document_type": "Letter",
                    "date": "1889-01-01",
                    "author": "Lovell, Ada Mary",
                    "recipient": "",
                    "place": "Albany, N.Y.",
                    "subjects": "",
                },
                ocr_draft_text="ocr",
                output_json={"document_id": document_id},
                cost_estimate=0.001,
            )
    return settings, batch_id


@pytest.mark.parametrize("second", ["E2241AAB", "E2241AAC"])
def test_merge_preserves_orientations_by_scan_and_renumbers_other_documents(tmp_path, stub_model, second):
    settings, _ = _drafted_folder(tmp_path)
    untouched = "E2241AAC" if second == "E2241AAB" else "E2241AAB"
    with db.connect(settings.db_path) as conn:
        db.set_page_orientation(conn, "new", "E2241AAA", 1, 90, False, "Paul")
        db.set_page_orientation(conn, "new", second, 0, 180, True, "John")
        db.set_page_orientation(conn, "new", untouched, 0, 270, False, "Ada")
        conn.execute("UPDATE page_orientations SET set_at = '2026-10-01T12:00:00+00:00'")
    result = merge.merge_documents(["E2241AAA", second], settings)
    with db.connect(settings.db_path) as conn:
        assert db.page_orientations(conn, "new", result["document_id"]) == {
            1: {"rotation": 90, "mirrored": False}, 2: {"rotation": 180, "mirrored": True}}
        assert db.page_orientations(conn, "new", untouched) == {
            0: {"rotation": 270, "mirrored": False}}
        saved = conn.execute("SELECT set_by, set_at FROM page_orientations WHERE document_id = ? "
                             "AND sequence_index = 2", (result["document_id"],)).fetchone()
        assert tuple(saved) == ("John", "2026-10-01T12:00:00+00:00")


def test_resplit_moves_orientations_to_the_new_page_indexes(tmp_path):
    settings, _ = _drafted_folder(tmp_path)
    with db.connect(settings.db_path) as conn:
        db.set_page_orientation(conn, "new", "E2241AAA", 1, 90, False, "Paul")
        db.set_page_orientation(conn, "new", "E2241AAB", 0, 180, True, "John")
    split.resplit_documents(["E2241AAA", "E2241AAB"], [
        {"display_id": "E2241AAA", "sequence_indices": [0]},
        {"display_id": "E2241AAB", "sequence_indices": [1, 2]},
    ], settings)
    with db.connect(settings.db_path) as conn:
        assert db.page_orientations(conn, "new", document_named(conn, "E2241AAA")) == {}
        assert db.page_orientations(conn, "new", document_named(conn, "E2241AAB")) == {
            0: {"rotation": 90, "mirrored": False}, 1: {"rotation": 180, "mirrored": True}}


def test_resplit_captures_orientations_under_its_rebuild_transaction(tmp_path, monkeypatch):
    settings, _ = _drafted_folder(tmp_path)
    original = split.carry_intake_orientations
    captured = []

    def capture(conn, images):
        assert conn.in_transaction
        captured.append(True)
        return original(conn, images)

    monkeypatch.setattr(split, "carry_intake_orientations", capture)
    split.resplit_documents(["E2241AAA"], [
        {"display_id": "E2241AAA", "sequence_indices": [0, 1]},
    ], settings)
    assert captured == [True]


def test_resplit_with_a_missing_intake_scan_preserves_the_existing_documents(tmp_path):
    settings, batch_id = _drafted_folder(tmp_path)
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        key = db.get_batch_images(conn, batch_id)[0]["bucket_key"]
        original_pages = [dict(p) for p in db.get_new_document_pages(conn, "E2241AAA")]
        db.set_page_orientation(conn, "new", "E2241AAA", 0, 90, False, "Paul")
    storage.delete_keys([key])
    with pytest.raises(ValueError, match="intake scan.*missing"):
        split.resplit_documents(["E2241AAA"], [
            {"display_id": "E2241AAA", "sequence_indices": [0, 1]},
        ], settings)
    with db.connect(settings.db_path) as conn:
        assert [dict(p) for p in db.get_new_document_pages(conn, "E2241AAA")] == original_pages
        assert db.page_orientations(conn, "new", "E2241AAA") == {0: {"rotation": 90, "mirrored": False}}
    assert all(storage.exists(page["bucket_key"]) for page in original_pages)


def test_resplit_copy_failure_leaves_existing_page_files_and_rows(tmp_path, monkeypatch):
    settings, _ = _drafted_folder(tmp_path)
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        original = [dict(p) for p in db.get_new_document_pages(conn, "E2241AAA")]
    original_bytes = {p["bucket_key"]: storage.read_bytes(p["bucket_key"]) for p in original}

    original_copy = BucketStorage.copy
    calls = 0

    def fail_copy(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated copy failure")
        return original_copy(self, *args, **kwargs)

    monkeypatch.setattr(BucketStorage, "copy", fail_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        split.resplit_documents(["E2241AAA"], [
            {"display_id": "E2241AAA", "sequence_indices": [0, 1]},
        ], settings)
    with db.connect(settings.db_path) as conn:
        assert [dict(p) for p in db.get_new_document_pages(conn, "E2241AAA")] == original
    assert {key: storage.read_bytes(key) for key in original_bytes} == original_bytes


def test_merge_copy_failure_leaves_both_source_documents_readable(tmp_path, stub_model, monkeypatch):
    settings, _ = _drafted_folder(tmp_path)
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        original = {document_id: [dict(p) for p in db.get_new_document_pages(conn, document_id)]
                    for document_id in ("E2241AAA", "E2241AAB")}
    keys = {p["bucket_key"] for pages in original.values() for p in pages}
    original_bytes = {key: storage.read_bytes(key) for key in keys}

    original_copy = BucketStorage.copy
    calls = 0

    def fail_copy(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated copy failure")
        return original_copy(self, *args, **kwargs)

    monkeypatch.setattr(BucketStorage, "copy", fail_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        merge.merge_documents(["E2241AAA", "E2241AAB"], settings)
    with db.connect(settings.db_path) as conn:
        assert {document_id: [dict(p) for p in db.get_new_document_pages(conn, document_id)]
                for document_id in original} == original
    assert {key: storage.read_bytes(key) for key in keys} == original_bytes


@pytest.fixture
def stub_model(monkeypatch):
    """Replaces the one model call a merge makes, recording what it was sent."""
    calls = []

    def fake_call(messages, settings, schema=None, extract_result=None):
        calls.append({"messages": messages, "schema": schema})
        return extract_result(
            {
                "document_type": "Letter",
                "date": "1889-03-14",
                "author": "Hayes, Howard W",
                "recipient": "Lovell, Ada Mary",
                "place": "Newark, N.J.",
                "names": ["Marsh, Henry"],
                "subjects": ["Patents"],
            },
            {"cost": 0.0002},
        )

    monkeypatch.setattr(merge.openrouter, "call_openrouter", fake_call)
    return calls


# ---------- what a merge preserves and what it regenerates ----------


def test_merge_keeps_every_page_of_transcription(tmp_path, stub_model):
    """The whole reason merging isn't a re-draft: the words on a page don't change because the page
    turned out to belong to a longer document."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    assert "text of E2241AAA" in row["text"]
    assert "text of E2241AAB" in row["text"]
    # Joined in scan order, not selection order.
    assert row["text"].index("text of E2241AAA") < row["text"].index("text of E2241AAB")


def test_merge_regenerates_only_the_document_level_metadata(tmp_path, stub_model):
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    # Regenerated from the joined text -- the fragments each had a different (wrong) answer.
    drafted = fields.drafted(row)
    assert (drafted["author"], drafted["recipient"], drafted["date"], drafted["place"]) ==         ("Hayes, Howard W", "Lovell, Ada Mary", "1889-03-14", "Newark, N.J.")
    # The Mentioned index: what the pages found, plus the merge's own reading of the whole text.
    assert drafted["names"] == ["Clerk, E2241AAA", "Clerk, E2241AAB", "Marsh, Henry"]
    assert drafted["subjects"] == ["Patents"]
    # Derived title tracks the regenerated metadata, not the old fragment's.
    assert "E2241AAA" in row["ai_title"]
    assert "Hayes" in row["ai_title"]


def test_merge_sends_the_joined_text_and_no_images(tmp_path, stub_model):
    """A merge must be text-only."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    content = stub_model[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text"]
    assert "text of E2241AAA" in content[0]["text"]
    assert "text of E2241AAB" in content[0]["text"]
    # The metadata-only schema, so the model structurally cannot return replacement transcription.
    properties = stub_model[0]["schema"]["json_schema"]["schema"]["properties"]
    assert "text" not in properties
    assert set(properties) == {
        "document_type", "date", "author", "recipient", "place", "names", "subjects",
    }


def test_a_merge_makes_exactly_one_model_call(tmp_path, stub_model):
    """The transcription is carried, not redrafted, so only the document-level metadata is worth
    asking for -- and asking for it once."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    assert len(stub_model) == 1


def test_merging_more_documents_does_not_cost_more_calls(tmp_path, stub_model):
    """The cost of a merge is one request against the joined text, not one per fragment."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB", "E2241AAC"], settings)

    assert len(stub_model) == 1


def test_the_only_call_a_merge_makes_is_the_metadata_one(tmp_path, stub_model):
    """No redraft and no second pass over the names: the fragments were drafted and their names
    resolved already, and joining them changes neither."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    assert [call["schema"] for call in stub_model] == [prompting.compile({}).merge_schema]


def test_merge_prefers_a_reviewers_corrected_text(tmp_path, stub_model):
    """A reviewer who fixed a misread word was right about that word regardless of where the
    document boundary turned out to be, so that correction is carried forward."""
    settings, _ = _drafted_folder(tmp_path)
    with db.connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE new_documents SET reviewed_text = ? WHERE document_id = ?",
            ("the reviewer's corrected text", "E2241AAA"),
        )

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    assert "the reviewer's corrected text" in row["text"]
    assert "text of E2241AAA" not in row["text"]


def test_merge_returns_the_document_to_unreviewed(tmp_path, stub_model):
    """Reviewing these as separate documents was itself the mistake being corrected, so the merged
    document has to be read again as one document before it can be exported."""
    settings, _ = _drafted_folder(tmp_path)
    with db.connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE new_documents SET review_status = 'accepted', reviewer = 'jo' "
            "WHERE document_id IN ('E2241AAA', 'E2241AAB')"
        )

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    assert row["review_status"] == "unreviewed"
    assert row["status"] == "drafted"


def test_merge_dedupes_names_repeated_across_the_fragments(tmp_path, stub_model):
    """Every fragment of one letter names the same correspondents; keeping all the copies would
    make the merged document look like it names four different people."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    names = fields.drafted(row)["names"]
    assert names.count("Clerk, E2241AAA") == 1
    assert "Clerk, E2241AAB" in names
    assert "Lovell, Ada Mary" not in names   # the merged document's recipient
    assert fields.drafted(row)["places"] == ["Albany, N.Y."]


def test_merge_joins_the_pages_in_scan_order(tmp_path, stub_model):
    settings, _ = _drafted_folder(tmp_path)

    result = merge.merge_documents(["E2241AAB", "E2241AAA"], settings)  # selected out of order

    assert result["document_id"] == "E2241AAA"  # the earliest, not the first ticked
    assert result["page_count"] == 3
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        pages = db.get_new_document_pages(conn, "E2241AAA")
        assert [p["sequence_index"] for p in pages] == [0, 1, 2]
        for page in pages:
            assert storage.exists(page["bucket_key"])
        # E2241AAB is gone; its name is back only because renumbering slid the next document onto it.
        assert result["renumbered"] == {"E2241AAC": "E2241AAB"}
        assert len(db.get_new_document_pages(conn, document_named(conn, "E2241AAB"))) == 3


def test_merge_records_what_it_was_built_from(tmp_path, stub_model):
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    output = json.loads(row["output_json"])
    assert output["source"]["merged_from"] == ["E2241AAA", "E2241AAB"]
    assert output["pipeline_info"]["method"] == "merge_documents"
    assert output["pipeline_info"]["prompt_version"] == "General correspondence v1"


# ---------- what a merge refuses ----------


def test_merge_refuses_a_single_document(tmp_path):
    settings, _ = _drafted_folder(tmp_path)
    with pytest.raises(ValueError, match="two or more"):
        merge.plan_merge(["E2241AAA"], settings)


def test_merge_accepts_documents_that_are_not_adjacent(tmp_path):
    """A reviewer can join related fragments without absorbing the document between them."""
    settings, _ = _drafted_folder(tmp_path)

    plan = merge.plan_merge(["E2241AAC", "E2241AAA"], settings)

    assert plan["document_ids"] == ["E2241AAA", "E2241AAC"]
    assert plan["target_document_id"] == "E2241AAA"
    assert plan["page_count"] == 5
    assert plan["renumber"] == {}


def test_non_adjacent_merge_leaves_middle_document_and_storage_in_place(tmp_path, stub_model):
    settings, _ = _drafted_folder(tmp_path)

    result = merge.merge_documents(["E2241AAA", "E2241AAC"], settings)

    assert result["document_id"] == "E2241AAA"
    assert result["renumbered"] == {}
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        merged = db.get_new_document(conn, "E2241AAA")
        middle = db.get_new_document(conn, "E2241AAB")
        assert db.get_new_document(conn, "E2241AAC") is None
        assert "text of E2241AAA" in merged["text"]
        assert "text of E2241AAC" in merged["text"]
        assert "text of E2241AAB" not in merged["text"]
        assert middle is not None
        assert {p["sequence_index"] for p in db.get_new_document_pages(conn, "E2241AAA")} == {
            0, 1, 2, 3, 4
        }
        assert all("/E2241AAA/" in p["bucket_key"]
                   for p in db.get_new_document_pages(conn, "E2241AAA"))
        assert all("/E2241AAB/" in p["bucket_key"]
                   for p in db.get_new_document_pages(conn, "E2241AAB"))
        folder_id = merged["folder_id"]
    assert json.loads(storage.read_record(folder_id, "E2241AAA"))["document_id"] == "E2241AAA"
    assert storage.read_record(folder_id, "E2241AAC") is None


def test_merge_refuses_an_undrafted_document(tmp_path, stub_model):
    """There is no transcription to keep, which is the entire premise of merging."""
    settings, _ = _drafted_folder(tmp_path)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET status = 'split_confirmed' WHERE document_id = 'E2241AAB'")

    with pytest.raises(ValueError, match="drafted first"):
        merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    # And it refused before spending anything or touching the folder.
    assert stub_model == []
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, "E2241AAB") is not None


def test_merge_refuses_documents_from_different_batches(tmp_path):
    settings, _ = _drafted_folder(tmp_path)
    other_batch = split.run_ingest_batch([("x.jpg", tiny_image_bytes())], settings, folder_display_id="E2241")
    with next_document_ids("E2241AAZ"):
        split.confirm_batch_split(other_batch, [{"display_id": "E2241AAZ", "sequence_indices": [0]}], settings)

    with pytest.raises(ValueError, match="different scan batches"):
        merge.plan_merge(["E2241AAA", "E2241AAZ"], settings)


def test_merge_refuses_once_the_spend_cap_is_reached(tmp_path, stub_model):
    settings, _ = _drafted_folder(tmp_path)
    bill(settings.db_path, 0.001)
    settings.max_total_cost = 0.0001  # already exceeded by the drafting above

    with pytest.raises(RuntimeError, match="cap"):
        merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    assert stub_model == []
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, "E2241AAB") is not None


def test_a_failed_model_call_leaves_the_folder_untouched(tmp_path, monkeypatch):
    """The metadata call happens before anything is deleted, so a merge either completes or is a
    no-op -- never a folder with the sources gone and no replacement."""
    settings, _ = _drafted_folder(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("openrouter is down")

    monkeypatch.setattr(merge.openrouter, "call_openrouter", boom)

    with pytest.raises(RuntimeError, match="down"):
        merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        for document_id in ("E2241AAA", "E2241AAB", "E2241AAC"):
            row = db.get_new_document(conn, document_id)
            assert row is not None and row["status"] == "drafted"


def test_merge_survives_a_malformed_list_cache(tmp_path, stub_model):
    """A merge must not fail because an older run left an unreadable JSON column -- the metadata is
    being regenerated anyway, and the transcription is the thing worth protecting."""
    settings, _ = _drafted_folder(tmp_path)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET fields_json = 'not json' WHERE document_id = 'E2241AAA'")

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    assert "text of E2241AAA" in row["text"]
    assert fields.drafted(row)["names"] == ["Clerk, E2241AAB", "Marsh, Henry"]


# ---------- closing the DocID gap ----------


def test_merge_closes_the_docid_gap_it_leaves(tmp_path, stub_model):
    """A gap in the sequence reads in the export CSV as a lost document rather than a correction."""
    settings, _ = _drafted_folder(tmp_path)

    result = merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    assert result["renumbered"] == {"E2241AAC": "E2241AAB"}
    with db.connect(settings.db_path) as conn:
        shown = sorted(r["display_id"] for r in conn.execute("SELECT display_id FROM new_documents"))
    assert shown == ["E2241AAA", "E2241AAB"]


def test_a_renumbered_document_announces_its_new_docid(tmp_path, stub_model):
    """Found while merging on the site: the folder list still read "[E2241AAC], Letter" on the
    document that renumbering had just moved to E2241AAB. The row is copied column for column, so
    the stored title kept the id it was written with -- and the queue, the export and the backup
    record all read that stored title."""
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        titles = {row["display_id"]: row["ai_title"]
                  for row in conn.execute("SELECT display_id, ai_title FROM new_documents")}

    assert titles["E2241AAB"].startswith("E2241AAB")
    assert "E2241AAC" not in titles["E2241AAB"]


def test_renumbering_leaves_the_rest_of_a_title_alone(tmp_path, stub_model):
    """Only the leading id is the site's to rewrite; the words after it may be a reviewer's."""
    settings, _ = _drafted_folder(tmp_path)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET title_override = ? WHERE document_id = ?",
                     ("[E2241AAC], the clerk's own docket, kept verbatim", "E2241AAC"))

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT title_override FROM new_documents WHERE display_id = 'E2241AAB'").fetchone()

    assert row["title_override"] == "[E2241AAB], the clerk's own docket, kept verbatim"


def test_renumbering_changes_a_documents_display_id_and_nothing_else(tmp_path, stub_model):
    settings, _ = _drafted_folder(tmp_path)

    merge.merge_documents(["E2241AAA", "E2241AAB"], settings)

    with db.connect(settings.db_path) as conn:
        # E2241AAC is now shown as E2241AAB, under the same internal id, with all three pages.
        assert db.display_id_of(conn, "E2241AAC") == "E2241AAB"
        assert [p["sequence_index"] for p in db.get_new_document_pages(conn, "E2241AAC")] == [0, 1, 2]
        assigned = {
            row["sequence_index"]: row["assigned_document_id"]
            for row in db.get_batch_images(conn, result_batch(conn))
        }
    assert assigned == {0: "E2241AAA", 1: "E2241AAA", 2: "E2241AAA", 3: "E2241AAC", 4: "E2241AAC", 5: "E2241AAC"}


def result_batch(conn):
    return conn.execute("SELECT batch_id FROM new_batches").fetchone()["batch_id"]


def test_renumbering_survives_a_shift_that_collides_with_itself(tmp_path):
    """The plan's targets overlap its own sources (AAF->AAC, AAG->AAD), so renaming one at a time
    would write an id another row in the same plan still holds."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i}.jpg", tiny_image_bytes()) for i in range(3)], settings,
                                      folder_display_id="E2241")
    with next_document_ids("d-aaa", "d-aaf", "d-aag"):
        split.confirm_batch_split(
            batch_id,
            [
                {"display_id": "E2241AAA", "sequence_indices": [0]},
                {"display_id": "E2241AAF", "sequence_indices": [1]},
                {"display_id": "E2241AAG", "sequence_indices": [2]},
            ],
            settings,
        )

    applied = merge.renumber_folder(_folder_id(settings), settings)

    assert applied == {"d-aaf": "E2241AAB", "d-aag": "E2241AAC"}
    with db.connect(settings.db_path) as conn:
        shown = sorted(r["display_id"] for r in conn.execute("SELECT display_id FROM new_documents"))
    assert shown == ["E2241AAA", "E2241AAB", "E2241AAC"]


def test_renumbering_a_dense_folder_changes_nothing(tmp_path):
    settings, _ = _drafted_folder(tmp_path)
    assert merge.renumber_folder(_folder_id(settings), settings) == {}


def _folder_id(settings):
    with db.connect(settings.db_path) as conn:
        return db.find_folder(conn, "E2241")["folder_id"]


def test_renumber_plan_skips_ids_from_another_collection(tmp_path):
    """`X018A4-0242` follows no folder sequence, so it is stepped over rather than renumbered into
    one."""
    plan = docid.renumber_plan("E2241", ["E2241AAA", "X018A4-0242", "E2241AAC"])
    assert plan == {"E2241AAC": "E2241AAB"}
    assert "X018A4-0242" not in plan
