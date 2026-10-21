"""Taking a folder back from the reviewer holding it."""

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, db, review_server
from incipit.config import Settings

STATIC = "app/static"


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, db_path


def client(app, username=None, password=None) -> TestClient:
    c = TestClient(app)
    c.cookies.clear()
    if username:
        c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    return c


def uid(db_path, username):
    with db.connect(db_path) as conn:
        return accounts.get_user_by_name(conn, username)["id"]


def assign(app, db_path, folder="E2002"):
    admin = client(app, "paul", "paul-password")
    admin.post("/api/team/assign", json={
        "folder_id": folder, "workflow": "new", "reviewer_id": uid(db_path, "john")})
    return admin


def test_an_admin_takes_a_folder_back_and_learns_whose_it_was(site):
    app, db_path = site
    admin = assign(app, db_path)

    response = admin.post("/api/team/unassign", json={"folder_id": "E2002", "workflow": "new"})

    assert response.status_code == 200
    assert response.json()["reviewer"] == "John"


def test_the_folder_leaves_the_reviewers_queue(site):
    app, db_path = site
    admin = assign(app, db_path)
    john = client(app, "john", "john-password")
    assert [f["folder_id"] for f in john.get("/api/my/folders").json()["folders"]] == ["E2002"]

    admin.post("/api/team/unassign", json={"folder_id": "E2002", "workflow": "new"})

    assert john.get("/api/my/folders").json()["folders"] == []


def test_a_reviewer_cannot_unassign_a_folder(site):
    app, db_path = site
    assign(app, db_path)
    john = client(app, "john", "john-password")

    assert john.post("/api/team/unassign",
                     json={"folder_id": "E2002", "workflow": "new"}).status_code == 403


def test_unassigning_a_folder_nobody_holds_says_so(site):
    app, db_path = site
    admin = client(app, "paul", "paul-password")

    response = admin.post("/api/team/unassign", json={"folder_id": "E2002", "workflow": "new"})

    assert response.status_code == 400
    assert "not assigned" in response.json()["detail"]


def test_the_reviewers_own_verdicts_survive_being_unassigned(site):
    app, db_path = site
    admin = assign(app, db_path)
    with db.connect(db_path) as conn:
        db.set_document_review(conn, "E2002AA", "new", "reviewer", "accepted", "John")

    admin.post("/api/team/unassign", json={"folder_id": "E2002", "workflow": "new"})

    with db.connect(db_path) as conn:
        assert db.get_role_verdicts(conn, "new", "reviewer")["E2002AA"]["status"] == "accepted"


def test_taking_a_folder_back_is_recorded_against_the_folder(site):
    app, db_path = site
    admin = assign(app, db_path)

    admin.post("/api/team/unassign", json={"folder_id": "E2002", "workflow": "new"})

    with db.connect(db_path) as conn:
        kinds = [r["action"] for r in accounts.folder_history(conn, "E2002", "new")]
    assert "unassigned" in kinds


def test_an_unknown_workflow_is_refused(site):
    app, db_path = site
    admin = assign(app, db_path)

    response = admin.post("/api/team/unassign", json={"folder_id": "E2002", "workflow": "nonsense"})

    assert response.status_code == 400


def test_the_queue_offers_unassign_in_the_more_menu():
    source = open(f"{STATIC}/app.js", encoding="utf-8").read()
    assert "data-unassign" in source
    assert "/api/team/unassign" in source


def test_the_team_view_offers_unassign_on_every_assigned_row():
    source = open(f"{STATIC}/workspace.js", encoding="utf-8").read()
    assert "data-unassign" in source
    assert "window.__refreshTeam__ = loadRoster" in source
