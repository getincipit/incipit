"""What crosses between a reviewer and a reviewer, swept endpoint by endpoint."""

import json

import pytest

from incipit import accounts, db, review_server
from incipit.config import Settings
from conftest import insert_rows, signed_in


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        paul = accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        john = accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B', 'now', 3, 'E2002')")
        for i, status in enumerate(["accepted", "flagged", "unreviewed"]):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status, "
                "cost_estimate) VALUES (?, 'B', 'drafted', ?, 0.0123)",
                (f"E2002AA{chr(ord('A') + i)}", status))
        accounts.assign_folder(conn, "E2002", "new", john, paul)
        db.set_document_review(conn, "E2002AAA", "new", "admin", "accepted", "Paul")
        db.set_document_review(conn, "E2002AAB", "new", "admin", "flagged", "Paul")
        db.record_folder_export(conn, "E2002", "new", 1, "Paul")
    return application, db_path


def reviewer_folder(application):
    return signed_in(application, "john", "john-password").get("/api/new/folders").json()[0]


# ---------- the folder listing ----------


def test_the_reviewers_running_count_is_not_sent_to_the_reviewer(app):
    """The reported leak, read from the payload rather than from the page."""
    application, _ = app
    folder = reviewer_folder(application)

    assert "accepted_count" not in folder
    assert "all_approved" not in folder


def test_the_reviewers_own_progress_is_still_sent(app):
    """Stripping the reviewer's numbers must not cost them their own bar."""
    application, _ = app
    folder = reviewer_folder(application)

    assert folder["progress"]["viewer"]["total"] == 3
    assert folder["document_count"] == 3


def test_the_reviewers_progress_carries_no_breakdown_by_role(app):
    application, _ = app
    progress = reviewer_folder(application)["progress"]

    assert "by_role" not in progress
    assert "other" not in progress


def test_a_reviewers_flag_is_not_counted_into_the_reviewers_own_numbers(app):
    """The subtler half: merged counts said "1 flagged" on the reviewer's card."""
    application, _ = app
    progress = reviewer_folder(application)["progress"]

    assert progress["needs_work"] == 0
    assert progress["accepted"] == 0


def test_the_export_and_claim_state_stay_with_the_reviewer(app):
    application, _ = app
    folder = reviewer_folder(application)

    assert "export" not in folder and "claim" not in folder


def test_what_a_folder_cost_to_draft_is_not_the_reviewers_business(app):
    application, _ = app
    assert "cost_estimate" not in reviewer_folder(application)


def test_the_reviewer_still_receives_all_of_it(app):
    """The narrowing is for reviewers only -- an admin's card is unchanged."""
    application, _ = app
    folder = signed_in(application, "paul", "paul-password").get("/api/new/folders").json()[0]

    for field in ("accepted_count", "all_approved", "export", "claim", "cost_estimate"):
        assert field in folder, field
    assert folder["progress"]["other"]["total"] == 3


# ---------- one document ----------


def test_a_reviewer_sees_their_own_verdict_not_the_reviewers(app):
    application, _ = app
    john = signed_in(application, "john", "john-password")

    assert john.get("/api/new/documents/E2002AAA").json()["review"]["status"] == "unreviewed"
    assert john.get("/api/new/documents/E2002AAB").json()["review"]["status"] == "unreviewed"


def test_the_document_queue_shows_a_reviewer_only_their_own_verdicts(app):
    application, _ = app
    rows = signed_in(application, "john", "john-password").get("/api/new/documents").json()

    assert {r["review_status"] for r in rows} == {"unreviewed"}


def test_what_a_draft_cost_is_not_sent_to_a_reviewer(app):
    application, _ = app
    john = signed_in(application, "john", "john-password")

    assert john.get("/api/new/documents/E2002AAA").json()["pipeline_info"]["cost_estimate"] is None


def test_the_reviewer_still_sees_the_cost_and_their_own_verdicts(app):
    application, _ = app
    paul = signed_in(application, "paul", "paul-password")
    document = paul.get("/api/new/documents/E2002AAA").json()

    assert document["pipeline_info"]["cost_estimate"] == pytest.approx(0.0123)
    assert document["review"]["status"] == "accepted"


# ---------- the admin-only surfaces, which must stay shut ----------


@pytest.mark.parametrize("path", [
    "/api/team/roster", "/api/team/roster.csv", "/api/team/history?folder_id=E2002",
    "/api/analytics/folder?folder_id=E2002&workflow=new", "/api/settings",
])
def test_the_reviewers_own_screens_are_refused_outright(app, path):
    application, _ = app
    assert signed_in(application, "john", "john-password").get(path).status_code == 403


def test_a_reviewer_cannot_export_a_folder(app):
    application, _ = app
    response = signed_in(application, "john", "john-password").post(
        "/api/folder-exports/new/E2002")

    assert response.status_code in (403, 404, 405)


def test_no_reviewer_verdict_words_reach_a_reviewer_anywhere_in_the_folder_payload(app):
    """A belt-and-braces sweep: whatever else the payload grows, none of it may spell out how the
    reviewer ruled."""
    application, _ = app
    body = json.dumps(reviewer_folder(application))

    assert "accepted_count" not in body
    assert "all_approved" not in body
    assert "admin" not in body


# ---------- other reviewers' folders ----------


def _another_folder(db_path):
    with db.connect(db_path) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('C', 'now', 1, 'E2003')")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status, "
                     "ai_title) VALUES ('E2003AAA', 'C', 'drafted', 'unreviewed', "
                     "'someone else''s letter')")


def test_a_reviewers_queue_does_not_carry_another_folders_documents(app):
    """Found while clearing stale code: /api/new/documents narrowed nothing, so a reviewer holding
    E2002 was sent E2003's ids and titles too."""
    application, db_path = app
    _another_folder(db_path)

    queue = signed_in(application, "john", "john-password").get("/api/new/documents").json()

    assert {d["document_id"] for d in queue} == {"E2002AAA", "E2002AAB", "E2002AAC"}


def test_a_reviewer_still_receives_every_folders_documents(app):
    application, db_path = app
    _another_folder(db_path)

    queue = signed_in(application, "paul", "paul-password").get("/api/new/documents").json()

    assert "E2003AAA" in {d["document_id"] for d in queue}


def test_an_expired_session_is_cleared_when_the_next_one_starts(app):
    """Nothing ever deleted a session, so the table only grew."""
    _, db_path = app
    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, "
                     "last_seen_at) VALUES ('stale', 1, '2000-01-01', '2000-01-02', '2000-01-01')")
        accounts.start_session(conn, 1)

        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash = 'stale'"
                            ).fetchone()[0] == 0
