"""The queue keeps itself current, and never at the cost of someone's unsaved work."""
import pathlib

import pytest

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from starlette.testclient import TestClient
from conftest import insert_rows

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer")
    return app, db_path


def user(db_path, username):
    with db.connect(db_path) as conn:
        return accounts.get_user_by_name(conn, username)


def version_for(db_path, username):
    with db.connect(db_path) as conn:
        return collab.workspace_version(conn, accounts.get_user_by_name(conn, username))


# ---------- the signal ----------


def test_a_reviewers_first_assignment_changes_their_fingerprint(site):
    """The case an event-based signal cannot see."""
    app, db_path = site
    before = version_for(db_path, "john")
    with db.connect(db_path) as conn:
        accounts.assign_folder(conn, "E2226", "new", user(db_path, "john")["id"],
                               user(db_path, "paul")["id"])
    assert version_for(db_path, "john") != before


def test_losing_a_folder_changes_the_fingerprint_too(site):
    """Reassignment away is a change to that reviewer's queue, not only to the new holder's."""
    app, db_path = site
    john, jack, paul = (user(db_path, n)["id"] for n in ("john", "jack", "paul"))
    with db.connect(db_path) as conn:
        accounts.assign_folder(conn, "E2226", "new", john, paul)
    held = version_for(db_path, "john")
    with db.connect(db_path) as conn:
        accounts.assign_folder(conn, "E2226", "new", jack, paul)
    assert version_for(db_path, "john") != held


def test_one_reviewers_assignment_does_not_disturb_another(site):
    """A fingerprint that moved for everyone would make every page redraw for every assignment."""
    app, db_path = site
    john, paul = user(db_path, "john")["id"], user(db_path, "paul")["id"]
    before = version_for(db_path, "jack")
    with db.connect(db_path) as conn:
        accounts.assign_folder(conn, "E2226", "new", john, paul)
    assert version_for(db_path, "jack") == before


def test_the_fingerprint_is_stable_when_nothing_happens(site):
    """It is compared on every poll; one that drifted on its own would redraw the queue forever."""
    app, db_path = site
    with db.connect(db_path) as conn:
        accounts.assign_folder(conn, "E2226", "new", user(db_path, "john")["id"],
                               user(db_path, "paul")["id"])
    assert version_for(db_path, "john") == version_for(db_path, "john")


def test_the_poll_the_bell_already_runs_carries_the_fingerprint(site):
    """One request, not a second poll loop asking the same question."""
    app, db_path = site
    c = TestClient(app)
    c.cookies.clear()
    c.post("/login", data={"username": "john", "password": "john-password"}, follow_redirects=False)
    payload = c.get("/api/notifications").json()
    assert "version" in payload


# ---------- and it cannot cost anyone their work ----------


def test_nothing_in_the_workspace_ever_reloads_the_page():
    """A reload would discard an unsaved transcription without asking."""
    for name in ("workspace.js", "app.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "location.reload" not in source, f"{name} can reload the page out from under someone"


def test_the_refresh_stands_down_while_a_document_is_open():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert "function refreshQueueIfSafe" in source
    body = source.split("function refreshQueueIfSafe", 1)[1].split("\n  }", 1)[0]
    assert "if (detailIsOpen()) return;" in body, (
        "the queue can now be redrawn underneath someone who is editing a document"
    )


def test_a_change_seen_during_editing_is_remembered_rather_than_dropped():
    """Standing down must not mean forgetting: the folder still has to appear once they are done."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert "queueIsStale = true" in source
    assert "#back-to-queue" in source, "nothing re-applies the deferred refresh on the way back"


def test_the_queue_redraw_is_actually_reachable():
    """app.js defines loadQueue and openFolder inside its IIFE."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "window.loadQueue = loadQueue;" in source
    assert "window.openFolder = openFolder;" in source


# ---------- Draft Pending: a reviewer may spend, but only on their own folder ----------


def _pending_document(conn, folder_id, suffix="AAA"):
    insert_rows(conn,
        "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
        "VALUES (?, 'now', 1, ?)", (f"B-{folder_id}", folder_id),
    )
    insert_rows(conn,
        "INSERT OR REPLACE INTO new_documents (document_id, batch_id, status) "
        "VALUES (?, ?, 'split_confirmed')", (f"{folder_id}{suffix}", f"B-{folder_id}"),
    )


def test_a_reviewers_draft_run_stays_inside_their_folders(site, monkeypatch):
    """Draft Pending drafts from every pending document in the database."""
    app, db_path = site
    from incipit import review_server as rs

    john, paul = user(db_path, "john")["id"], user(db_path, "paul")["id"]
    with db.connect(db_path) as conn:
        _pending_document(conn, "E2226")          # john's
        _pending_document(conn, "E2229")          # nobody's
        accounts.assign_folder(conn, "E2226", "new", john, paul)

    drafted = {}

    def fake_run(settings_, options, document_ids=None, **kw):
        drafted["ids"] = document_ids
        return None

    monkeypatch.setattr(rs, "run_transcribe_new_documents", fake_run)

    c = TestClient(app)
    c.cookies.clear()
    c.post("/login", data={"username": "john", "password": "john-password"}, follow_redirects=False)
    assert c.post("/api/new/draft-pending").status_code == 200

    assert drafted["ids"] is not None, "a reviewer's run was left unscoped"
    assert all(d.startswith("E2226") for d in drafted["ids"]), (
        f"a reviewer's draft run reached outside their folders: {drafted['ids']}"
    )


def test_an_admins_draft_run_is_left_exactly_as_it_was(site, monkeypatch):
    """An admin sees everything, so scoping must not narrow what their click drafts: the job covers
    every pending document under the configured limit."""
    app, db_path = site
    from incipit import review_server as rs

    with db.connect(db_path) as conn:
        _pending_document(conn, "E2226")

    seen = {}

    def fake_run(settings_, options, document_ids=None, **kw):
        seen["document_ids"] = document_ids
        return None

    monkeypatch.setattr(rs, "run_transcribe_new_documents", fake_run)

    c = TestClient(app)
    c.cookies.clear()
    c.post("/login", data={"username": "paul", "password": "paul-password"}, follow_redirects=False)
    assert c.post("/api/new/draft-pending").status_code == 200
    assert seen["document_ids"] == ["E2226AAA"], "an admin's run was narrowed"
