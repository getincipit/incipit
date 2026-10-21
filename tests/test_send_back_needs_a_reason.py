"""Sending a folder back always carries a reason for the reviewer."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, db, review_server
from incipit.config import Settings
from conftest import insert_rows, seed_batch

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
WORKSPACE = (STATIC / "workspace.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        paul = accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        john = accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        seed_batch(conn, "B", 1, folder="E2002", name="E2002_Advice")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                     "VALUES ('E2002AAA', 'B', 'drafted', 'unreviewed')")
        accounts.assign_folder(conn, "E2002", "new", john, paul)
        accounts.set_folder_status(conn, "E2002", "new", "submitted",
                                   accounts.get_user(conn, john), "")
    return application


def admin(application):
    client = TestClient(application)
    client.post("/login", data={"username": "paul", "password": "paul-password", "next": "/"},
                follow_redirects=False)
    return client


def send_back(client, note):
    return client.post("/api/team/review", json={
        "folder_id": "E2002", "workflow": "new", "status": "returned", "note": note})


def test_sending_back_without_a_reason_is_refused(site):
    refused = send_back(admin(site), "")

    assert refused.status_code == 400
    assert "must enter a reason" in refused.json()["detail"]


def test_a_reason_of_only_spaces_is_no_reason(site):
    assert send_back(admin(site), "   ").status_code == 400


def test_sending_back_with_a_reason_goes_through(site):
    assert send_back(admin(site), "check the dates on pages 3-5").status_code == 200


def test_approving_still_needs_no_note(site):
    """Only sending back asks for a reason."""
    response = admin(site).post("/api/team/review", json={
        "folder_id": "E2002", "workflow": "new", "status": "approved", "note": ""})

    assert "must enter a reason" not in response.text


def test_the_dialog_refuses_an_empty_reason_even_with_documents_ticked():
    assert "if (!note) {" in WORKSPACE
    assert "if (!note && !picked.length)" not in WORKSPACE


def test_the_box_is_highlighted_and_the_message_shown():
    assert 'box.classList.add("is-invalid")' in WORKSPACE
    assert "You must enter a reason to send\n            this back to the reviewer." in WORKSPACE
    assert ".rework-note.is-invalid" in CSS


def test_the_field_says_it_is_required_before_anyone_tries():
    assert 'class="rework-required">required</span>' in WORKSPACE


def test_typing_a_reason_clears_the_warning():
    assert 'box.classList.remove("is-invalid")' in WORKSPACE
