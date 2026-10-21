"""Taking a review back takes the reviewer's name with it."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, db, review_server
from incipit.config import Settings
from conftest import insert_rows

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings, str(tmp_path / "export"), str(tmp_path / "logs"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "partha", "partha-password", "Partha Pediredla", "admin")
        insert_rows(conn,"INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, "
                     "folder_id) VALUES ('b1', 'now', 1, 'E2002')")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                     "VALUES ('E2002AAA', 'b1', 'drafted', 'unreviewed')")
    client = TestClient(app)
    client.post("/login", data={"username": "partha", "password": "partha-password"},
                follow_redirects=False)
    return client, db_path


def save(client, status):
    return client.post("/api/new/documents/E2002AAA/review", json={
        "reviewed_text": "a page", "marginal_notes": [], "places": [], "names": [],
        "challenges": [], "review_status": status, "editors_notes": "",
    })


def stored_reviewer(db_path):
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT review_status, reviewer FROM new_documents WHERE document_id = 'E2002AAA'"
        ).fetchone()
    return row["review_status"], (row["reviewer"] or "")


def test_accepting_a_document_records_who_did_it(site):
    client, db_path = site

    assert save(client, "accepted").status_code == 200

    assert stored_reviewer(db_path) == ("accepted", "Partha Pediredla")


def test_marking_it_unreviewed_takes_the_name_back_off(site):
    """The report: every unreviewed document claimed the last admin to touch it had reviewed it."""
    client, db_path = site
    save(client, "accepted")

    save(client, "unreviewed")

    assert stored_reviewer(db_path) == ("unreviewed", "")


def test_a_document_nobody_has_ruled_on_names_nobody(site):
    """Saving an edit is not a verdict, and must not sign the document."""
    client, db_path = site

    save(client, "unreviewed")

    assert stored_reviewer(db_path) == ("unreviewed", "")


def test_the_document_screen_only_says_it_when_it_is_true():
    """The line was drawn from the stored name alone, so a stale name kept showing."""
    start = APP.index("function renderReviewedBy(")
    block = APP[start:APP.index("\n  }", start)]

    assert "doc.review.status" in block, "the line has to check the verdict, not just the name"
    assert '=== "accepted"' in block


def test_taking_the_verdict_back_redraws_the_line():
    """The report: pressing Mark Unreviewed left "Reviewed by ..." standing underneath. Only the
    path that refetches the document redrew it; the one that patches state in place did not."""
    start = APP.index("  async function saveReview(")
    block = APP[start:APP.index('el("btn-save-draft")', start)]

    assert "renderReviewedBy(" in block


def test_the_line_is_emptied_rather_than_only_hidden():
    """A name left in the DOM is still read out, and is what would flash up next."""
    start = APP.index("function renderReviewedBy(")
    block = APP[start:APP.index("\n  }", start)]

    assert "reviewedByText.textContent = who ?" in block
