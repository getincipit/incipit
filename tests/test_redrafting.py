"""Drafting a document again: what it replaces, what it keeps, and what a failure leaves."""
import json

import pytest

from incipit import accounts, activity, db, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, signed_in, seed_draft


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B', 'now', 2, 'E2002')")
        for document_id in ("E2002AAA", "E2002AAB"):
            insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status, "
                         "text) VALUES (?, 'B', 'drafted', 'unreviewed', 'text')", (document_id,))
    activity.clear()
    yield app, settings
    activity.clear()


def review(status, text="text", **values):
    return {"reviewed_text": text, "review_status": status, "editors_notes": "", "title": "",
            "fields": {"document_type": "Letter", **values}}


@pytest.fixture
def redrafting(site, monkeypatch):
    """A saved review on E2002AAA, and a model that answers a redraft with a new reading."""
    app, settings = site
    signed_in(app, "paul").post("/api/new/documents/E2002AAA/review", json={
        **review("unreviewed", "the old reading", marginal_notes=["[Top, Page 1] old note"]),
        "title": "My own title"})

    def fake_run(settings, options, document_ids=None, progress=None):
        with db.connect(settings.db_path) as conn:
            seed_draft(
                conn, "E2002AAA", model="qwen", provider="openrouter",
                result={"text": "the new reading", "challenges": [], "uncertain_words": [],
                        "marginal_notes": ["[Across the text, Page 1] the reply"], "places": [],
                        "names": []},
                ai_metadata={"title": "AI title", "document_type": "Letter"},
                ocr_draft_text="", output_json={}, cost_estimate=0.0)
    monkeypatch.setattr(review_server, "run_transcribe_new_documents", fake_run)
    signed_in(app, "paul").post("/api/new/documents/E2002AAA/redraft")
    return signed_in(app, "paul").get("/api/new/documents/E2002AAA").json()


def test_a_redraft_shows_the_new_transcription_over_a_saved_one(redrafting):
    """Reported on E2023: once a review was saved, Redraft wrote a new draft the screen never showed."""
    assert redrafting["transcription"]["text"] == "the new reading"


def test_a_redraft_shows_the_new_marginal_notes(redrafting):
    assert redrafting["fields"]["marginal_notes"] == ["[Across the text, Page 1] the reply"]


def test_a_redraft_replaces_what_is_stored_not_only_what_is_shown(site, redrafting):
    """The screen reads the reviewed copy first, so a redraft that only wrote the drafted column
    would show the new text and export the old one."""
    _, settings = site
    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "E2002AAA")

    assert row["text"] == "the new reading"
    assert row["reviewed_text"] is None




def test_a_redraft_rewrites_the_record_the_backup_copies(site, redrafting):
    """`data/bucket/records` is what the backup and the mirror both read; a stale record there is
    a stale document on both destinations."""
    _, settings = site
    storage = BucketStorage(settings.bucket_uri)

    record = json.loads(storage.read_record("E2002", "E2002AAA") or "{}")

    assert record["transcription"]["text"] == "the new reading"


def test_a_redraft_keeps_the_reviewers_metadata(redrafting):
    assert redrafting["metadata"]["title"] == "My own title"


@pytest.fixture
def failed_redraft(site, monkeypatch):
    """A drafted document whose redraft the model fails."""
    app, settings = site

    def fake_run(settings, options, document_ids=None, progress=None):
        with db.connect(settings.db_path) as conn:
            db.upsert_new_document_error(conn, "E2002AAA", "402 insufficient credits")
    monkeypatch.setattr(review_server, "run_transcribe_new_documents", fake_run)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET status = 'drafted' WHERE document_id = 'E2002AAA'")
    return signed_in(app, "paul").post("/api/new/documents/E2002AAA/redraft").json()


def test_a_failed_redraft_says_it_failed(failed_redraft):
    """It reported the old draft's length as the new one's, which the page read as success."""
    assert failed_redraft["drafted"] is False


def test_a_failed_redraft_says_why(failed_redraft):
    assert "402" in failed_redraft["error"]


def test_a_failed_redraft_keeps_the_document_in_the_export(site, failed_redraft):
    """Flipped to 'error', an accepted document dropped out of the export and into Draft Pending."""
    _, settings = site
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, "E2002AAA")["status"] == "drafted"


def test_a_review_save_is_refused_while_that_document_is_being_redrafted(site):
    app, _ = site
    client = signed_in(app, "paul")

    with activity.resource_lock("new-document:E2002AAA"):
        response = client.post("/api/new/documents/E2002AAA/review", json=review("unreviewed"))

    assert response.status_code == 409
    assert "being redrafted or saved" in response.json()["detail"]


def test_a_form_opened_before_a_redraft_cannot_overwrite_the_new_draft(site):
    app, settings = site
    client = signed_in(app, "paul")
    opened = client.get("/api/new/documents/E2002AAA").json()

    # This is the durable change a successful redraft makes to the form's version token.
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET run_date = 'after-redraft' "
                     "WHERE document_id = 'E2002AAA'")

    response = client.post(
        "/api/new/documents/E2002AAA/review",
        json={**review("unreviewed", "stale text"),
              "draft_version": opened["draft_version"]},
    )

    assert response.status_code == 409
    assert "redrafted after you opened it" in response.json()["detail"]
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, "E2002AAA")["reviewed_text"] is None
