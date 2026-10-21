"""A reviewer's flag counts as having ruled, and shows on the row as one."""

from pathlib import Path

import pytest

from incipit import accounts, collab, db
from conftest import insert_rows, seed_batch

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
WORKSPACE_JS = (STATIC / "workspace.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


@pytest.fixture
def folder(tmp_path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        seed_batch(conn, "B", 3, folder="E2013", name="E2013_Business")
        conn.execute("UPDATE new_batches SET status = 'confirmed' WHERE batch_id = 'B'")
        for document_id in ("E2013AAA", "E2013AAB", "E2013AAC"):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                "VALUES (?, 'B', 'drafted', 'unreviewed')", (document_id,))
    return db_path


def rule(db_path, document_id, status):
    with db.connect(db_path) as conn:
        db.set_document_review(conn, document_id, "new", "reviewer", status, "John Halloran")


def can_submit(db_path):
    with db.connect(db_path) as conn:
        return collab.check_can_submit(conn, "E2013", "new")


# ---------- handing the folder back ----------


def test_a_folder_of_flags_can_be_sent_for_review(folder):
    """A flag is the reviewer saying they have looked and it needs the reviewer."""
    for document_id in ("E2013AAA", "E2013AAB", "E2013AAC"):
        rule(folder, document_id, "needs_work")

    can_submit(folder)          # raises NotReady if it is refused


def test_a_mix_of_flags_and_accepts_can_be_sent(folder):
    rule(folder, "E2013AAA", "accepted")
    rule(folder, "E2013AAB", "needs_work")
    rule(folder, "E2013AAC", "accepted")

    can_submit(folder)


def test_a_document_nobody_has_ruled_on_still_stops_it(folder):
    """The rule the gate exists for: "submitted" has to mean every document was looked at."""
    rule(folder, "E2013AAA", "accepted")
    rule(folder, "E2013AAB", "needs_work")

    with pytest.raises(collab.NotReady, match="1 of 3"):
        can_submit(folder)


def test_the_refusal_says_what_is_still_needed(folder):
    with pytest.raises(collab.NotReady, match="accept each one first"):
        can_submit(folder)


def test_submission_uses_an_in_page_note_dialog_instead_of_a_blocking_prompt():
    """The embedded browser cannot implement window.prompt, and native prompts also block the
    page's event loop.  The handoff must work in the same UI as the rest of the workflow."""
    start = WORKSPACE_JS.index("function askSubmissionNote(")
    block = WORKSPACE_JS[start:WORKSPACE_JS.index("// ---------- the How It Works guides", start)]

    assert "prompt(" not in block
    assert 'overlay.className = "modal-overlay submit-overlay"' in block
    assert 'data-submit-note="send"' in block
    assert "await askSubmissionNote(" in block
    assert "if (note === null) return" in block


def test_a_flag_counts_towards_the_folders_progress(folder):
    rule(folder, "E2013AAA", "needs_work")

    with db.connect(folder) as conn:
        progress = collab.folder_progress(conn, "E2013", "new")

    assert progress["reviewed"] == 1
    assert progress["needs_work"] == 1
    assert progress["outstanding"] == 2


# ---------- and the row says so ----------


def test_a_document_needing_another_look_is_said_with_a_comment_not_a_pill():
    """Flagging is gone: what a document needs is written in its thread, and the folder's list
    shows the count on the row."""
    assert "pill-flagged" not in JS
    assert "queue-comments" in JS and ".queue-comment-count" in CSS


def test_the_reviewer_pill_is_one_shape_for_every_verdict():
    """Same class, so it inherits every shape rule -- only the colour differs."""
    start = JS.index("function reviewerPill(")
    block = JS[start:JS.index("\n  }", start)]

    assert 'class="pill ${verdict.css}"' in block


def test_nothing_is_shown_until_the_reviewer_has_ruled():
    """"Only when a document is genuinely flagged" -- an unreviewed document gets no pill."""
    start = JS.index("const REVIEWER_VERDICTS")
    block = JS[start:start + 260]

    assert "unreviewed" not in block
    assert "if (!verdict) return \"\";" in JS


def test_the_pill_says_who_and_what_in_its_tooltip():
    assert "${verdict.said} this document" in JS
