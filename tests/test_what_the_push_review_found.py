import sqlite3
"""Regressions from the pre-push review of 2026-09-29, one test per defect it reported."""

import pytest

from incipit import accounts, activity, collab, db, merge, review_server
from incipit.config import Settings
from conftest import insert_rows, signed_in


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


def assigned(app, settings, status="assigned"):
    with db.connect(settings.db_path) as conn:
        john = accounts.get_user_by_name(conn, "john")["id"]
    signed_in(app, "paul").post("/api/team/assign", json={
        "folder_id": "E2002", "workflow": "new", "reviewer_id": john})
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE folder_assignments SET status = ? WHERE folder_id = 'E2002'", (status,))


def review(status, text="text"):
    return {"reviewed_text": text, "marginal_notes": [], "places": [], "names": [],
            "challenges": [], "review_status": status, "editors_notes": "", "title": "",
            "document_type": "Letter", "date": "", "author": "", "recipient": "", "place": "",
            "subjects": ""}


def sent_back(settings, document_id="E2002AAA"):
    with db.connect(settings.db_path) as conn:
        collab.mark_documents_for_rework(conn, "E2002", "new", [document_id])


def test_an_admin_can_save_a_document_they_sent_back(site):
    app, settings = site
    sent_back(settings)
    response = signed_in(app, "paul").post("/api/new/documents/E2002AAA/review",
                                           json=review("needs_work", "edited"))
    assert response.status_code == 200


def test_saving_a_sent_back_document_keeps_it_marked(site):
    app, settings = site
    sent_back(settings)
    signed_in(app, "paul").post("/api/new/documents/E2002AAA/review", json=review("needs_work", "x"))
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, "E2002AAA")["review_status"] == "needs_work"


def test_needs_work_cannot_be_chosen_on_a_document_that_does_not_carry_it(site):
    app, _ = site
    response = signed_in(app, "paul").post("/api/new/documents/E2002AAA/review",
                                           json=review("needs_work"))
    assert response.status_code == 400


def test_opening_a_document_does_not_wait_on_a_writer(site):
    """The write lock belonged on the review POST; on the GET it made every open wait on a writer."""
    app, settings = site
    paul = signed_in(app, "paul")
    writer = sqlite3.connect(settings.db_path, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    try:
        response = paul.get("/api/new/documents/E2002AAA")
    finally:
        writer.rollback()
        writer.close()
    assert response.status_code == 200


def test_a_reviewer_cannot_edit_a_document_in_an_approved_folder(site):
    app, settings = site
    assigned(app, settings, status="approved")
    response = signed_in(app, "john").post("/api/new/documents/E2002AAA/review",
                                           json=review("accepted", "changed after approval"))
    assert response.status_code == 409


def test_a_reviewer_can_still_edit_a_folder_that_is_only_assigned(site):
    app, settings = site
    assigned(app, settings)
    response = signed_in(app, "john").post("/api/new/documents/E2002AAA/review",
                                           json=review("accepted"))
    assert response.status_code == 200


def test_an_admins_old_flag_does_not_reach_a_reviewer_as_sent_back(site):
    """Migrated Flag-button verdicts share the value a send-back leaves; only a returned folder counts."""
    app, settings = site
    assigned(app, settings)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = 'needs_work' WHERE document_id = 'E2002AAA'")
        db.set_document_review(conn, "E2002AAA", "new", "admin", "needs_work", "Paul")
        assert collab.documents_sent_back(conn, "E2002", "new") == set()


def test_a_real_send_back_still_reaches_the_reviewer(site):
    app, settings = site
    assigned(app, settings, status="returned")
    sent_back(settings)
    with db.connect(settings.db_path) as conn:
        assert collab.documents_sent_back(conn, "E2002", "new") == {"E2002AAA"}


def test_a_merge_cannot_start_while_its_folder_is_drafting(site):
    app, _ = site
    with activity.exclusive("drafting", "Drafting E2002", key="new:E2002"):
        response = signed_in(app, "paul").post(
            "/api/new/merge", json={"document_ids": ["E2002AAA", "E2002AAB"]})
    assert response.status_code == 409


def test_drafting_cannot_start_while_its_folder_is_being_merged():
    activity.clear()
    with activity.exclusive("confirming", "Merging", key="new:E2002", blocks=("drafting",)):
        with pytest.raises(activity.AlreadyRunning):
            with activity.exclusive("drafting", "Drafting E2002", key="new:E2002"):
                pass


def test_another_folder_drafts_while_one_is_merged():
    activity.clear()
    with activity.exclusive("confirming", "Merging", key="new:E2002", blocks=("drafting",)):
        with activity.exclusive("drafting", "Drafting E2003", key="new:E2003") as job:
            assert job["key"] == "new:E2003"


def test_a_merge_with_a_missing_scan_leaves_its_sources_in_place(site, monkeypatch):
    """The source documents were deleted before the scan copy that then failed."""
    _, settings = site
    monkeypatch.setattr(merge, "plan_merge", lambda ids, s: {
        "all_drafted": True, "document_ids": ["E2002AAA", "E2002AAB"],
        "target_document_id": "E2002AAA", "batch_id": "B", "folder_id": "E2002"})
    monkeypatch.setattr(merge, "resolve_intake_context", lambda conn, ids: ("B", [
        {"sequence_index": 0, "bucket_key": "uploads/B/0000.jpg", "assigned_document_id": "E2002AAA"}]))
    monkeypatch.setattr(merge.openrouter, "call_openrouter", lambda *a, **k: {
        "fields": {"document_type": "Letter"}, "cost": 0.0})
    with pytest.raises(ValueError):
        merge.merge_documents(["E2002AAA", "E2002AAB"], settings)
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, "E2002AAB") is not None


def reviewed_by(app, username="paul", document_id="E2002AAA"):
    return signed_in(app, username).get(f"/api/new/documents/{document_id}").json()["review"]["reviewed_by"]


def test_an_accepted_document_names_its_reviewer(site):
    app, _ = site
    signed_in(app, "paul").post("/api/new/documents/E2002AAA/review", json=review("accepted"))
    assert reviewed_by(app) == "Paul"


def test_unreviewing_a_document_removes_the_reviewers_name(site):
    app, _ = site
    paul = signed_in(app, "paul")
    paul.post("/api/new/documents/E2002AAA/review", json=review("accepted"))
    paul.post("/api/new/documents/E2002AAA/review", json=review("unreviewed"))
    assert reviewed_by(app) == ""


def test_a_sent_back_document_names_no_reviewer(site):
    """A send-back leaves the old accept's name in the column; the document is no longer reviewed."""
    app, settings = site
    signed_in(app, "paul").post("/api/new/documents/E2002AAA/review", json=review("accepted"))
    sent_back(settings)
    assert reviewed_by(app) == ""


def test_another_reviewer_confirming_a_document_is_named(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        accounts.create_user(conn, "mary", "mary-password", "Mary", "admin")
    paul = signed_in(app, "paul")
    paul.post("/api/new/documents/E2002AAA/review", json=review("accepted"))
    paul.post("/api/new/documents/E2002AAA/review", json=review("unreviewed"))
    signed_in(app, "mary").post("/api/new/documents/E2002AAA/review", json=review("accepted"))
    assert reviewed_by(app) == "Mary"


def test_an_unreviewed_document_exported_anyway_names_nobody(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET reviewer = 'Paul' WHERE document_id = 'E2002AAA'")
        rows = db.get_new_documents_for_export(conn, folder_id="E2002", include_unapproved=True)
    assert {row["document_id"]: row["reviewer"] for row in rows}["E2002AAA"] == ""


def test_submitting_a_folder_already_accepted_approves_it(site):
    """Reported in the 2026-09-30 smoke pass: nothing re-checked approval once the reviewer submitted."""
    app, settings = site
    assigned(app, settings)
    paul = signed_in(app, "paul")
    for document_id in ("E2002AAA", "E2002AAB"):
        paul.post(f"/api/new/documents/{document_id}/review", json=review("accepted"))
    john = signed_in(app, "john")
    for document_id in ("E2002AAA", "E2002AAB"):
        john.post(f"/api/new/documents/{document_id}/review", json=review("accepted"))

    john.post("/api/my/submit", json={"folder_id": "E2002", "workflow": "new"})
    with db.connect(settings.db_path) as conn:
        assert accounts.get_assignment(conn, "E2002", "new")["status"] == "approved"


def test_a_reviewer_is_told_a_document_in_an_approved_folder_is_locked(site):
    """Reported in the 2026-09-30 smoke pass: the page let the reviewer type, then refused every save."""
    app, settings = site
    assigned(app, settings, status="approved")
    assert signed_in(app, "john").get("/api/new/documents/E2002AAA").json()["locked"] is True


def test_an_admin_is_never_told_a_document_is_locked(site):
    app, settings = site
    assigned(app, settings, status="approved")
    assert signed_in(app, "paul").get("/api/new/documents/E2002AAA").json()["locked"] is False
