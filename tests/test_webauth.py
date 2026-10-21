"""The gate: who gets in, and what they may do once inside."""

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, db, review_server, webauth
from incipit.config import Settings
from conftest import insert_rows, signed_in


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer")
    return application, db_path


def anonymous(app) -> TestClient:
    """A client with no session -- conftest's fixture patches __init__ to sign in, so the cookies
    it plants are cleared here to get a genuinely unauthenticated caller."""
    client = TestClient(app)
    client.cookies.clear()
    return client


def test_presence_is_reported_to_admins_with_the_current_document(app):
    application, _ = app
    john = signed_in(application, "john", "john-password")
    paul = signed_in(application, "paul", "paul-password")

    assert john.post("/api/presence", json={
        "document_id": "E2002AAA", "workflow": "new",
    }).status_code == 200
    people = paul.get("/api/team/presence").json()["people"]

    [seen] = [person for person in people if person["name"] == "John"]
    assert seen["active"] is True
    assert seen["document_id"] == "E2002AAA"
    assert seen["workflow"] == "new"


def test_presence_roster_is_admin_only_but_reviewers_can_send_heartbeats(app):
    application, _ = app
    john = signed_in(application, "john", "john-password")

    assert john.post("/api/presence", json={}).status_code == 200
    assert john.get("/api/team/presence").status_code == 403


def test_a_malformed_presence_shape_is_harmless(app):
    application, _ = app
    john = signed_in(application, "john", "john-password")

    assert john.post("/api/presence", json=["not", "an", "object"]).status_code == 200
    [seen] = signed_in(application, "paul", "paul-password").get(
        "/api/team/presence").json()["people"]
    assert seen["document_id"] == ""


def seed_folder(db_path, folder_id="E2002", workflow="new", n=2, review_status="accepted"):
    """A folder with real documents in a given review state."""
    from incipit import db as _db
    with _db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
            "VALUES (?, 'now', ?, ?)", (f"B-{folder_id}", n, folder_id),
        )
        for i in range(n):
            suffix = chr(ord("A") + i // 26) + chr(ord("A") + i % 26)
            doc = f"{folder_id}A{suffix}"
            insert_rows(conn,
                "INSERT OR REPLACE INTO new_documents (document_id, batch_id, status, review_status) "
                "VALUES (?, ?, 'drafted', ?)", (doc, f"B-{folder_id}", review_status),
            )


# ---------- the gate is not optional ----------


def test_a_page_request_is_redirected_to_the_login_form(app):
    """An API caller gets 401 to react to; a person gets the login page rather than a bare error."""
    application, _ = app
    response = anonymous(application).get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_login_page_itself_is_reachable_without_a_session(app):
    application, _ = app
    assert anonymous(application).get("/login").status_code == 200


def test_login_next_is_html_escaped_and_cannot_inject_markup(app):
    application, _ = app
    hostile = '\" autofocus onfocus=\"alert(1)'

    response = anonymous(application).get("/login", params={"next": hostile})

    assert hostile not in response.text
    assert 'value="/"' in response.text


@pytest.mark.parametrize("target", [
    "https://attacker.example/collect",
    "//attacker.example/collect",
    "javascript:alert(1)",
    "/safe\r\nX-Injected: yes",
])
def test_login_never_redirects_off_site_or_accepts_control_characters(app, target):
    application, _ = app
    response = anonymous(application).post(
        "/login",
        data={"username": "john", "password": "john-password", "next": target},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_login_preserves_a_safe_local_destination(app):
    application, _ = app
    response = anonymous(application).post(
        "/login",
        data={"username": "john", "password": "john-password", "next": "/documents?filter=mine"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/documents?filter=mine"


def test_login_error_text_is_escaped_before_it_reaches_html():
    site = {"product_name": "Incipit", "labels": {"reviewer": "Reviewer"}, "join_enabled": True,
            "self_signup": False, "appearance": {}}
    rendered = webauth._login_html(site, error='<img src=x onerror="alert(1)">')

    # The page may have its own branding images; only the injected error must be escaped.
    error = rendered.split('<div class="auth-error">', 1)[1].split('</div>', 1)[0]
    assert "<img" not in error
    assert '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;' == error
    assert unescape(rendered).count('<img src=x onerror="alert(1)">') == 1


# ---------- signing in ----------


def test_a_correct_password_starts_a_session(app):
    application, _ = app
    client = signed_in(application, "john", "john-password")

    me = client.get("/api/me").json()
    assert me == {
        "id": me["id"], "username": "john", "display_name": "John", "role": "reviewer",
        "must_change_password": False,
    }
    assert isinstance(me["id"], int)


def test_a_wrong_password_does_not(app):
    application, _ = app
    client = anonymous(application)

    response = client.post("/login", data={"username": "john", "password": "wrong"},
                           follow_redirects=False)

    assert response.status_code == 401
    assert client.get("/api/me").status_code == 401


def test_the_session_cookie_is_not_readable_from_javascript(app):
    """HttpOnly is what stops an XSS bug anywhere in the app from lifting a session."""
    application, _ = app
    client = anonymous(application)
    response = client.post("/login", data={"username": "john", "password": "john-password"},
                           follow_redirects=False)

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_https_login_marks_the_session_cookie_secure(app):
    application, _ = app
    client = TestClient(application, base_url="https://testserver")
    client.cookies.clear()

    response = client.post(
        "/login", data={"username": "john", "password": "john-password"},
        follow_redirects=False,
    )

    assert "secure" in response.headers["set-cookie"].lower()


def test_logging_out_ends_the_session(app):
    application, _ = app
    client = signed_in(application, "john", "john-password")
    assert client.get("/api/me").status_code == 200

    client.post("/logout", follow_redirects=False)

    assert client.get("/api/me").status_code == 401


def test_logout_is_not_a_cross_site_friendly_get(app):
    application, _ = app
    client = signed_in(application, "john", "john-password")

    assert client.get("/logout", follow_redirects=False).status_code == 404
    assert client.get("/api/me").status_code == 200


def test_responses_carry_baseline_browser_security_headers(app):
    application, _ = app
    response = anonymous(application).get("/login")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_repeated_failed_logins_are_rate_limited(app, monkeypatch):
    application, _ = app
    client = anonymous(application)
    calls = 0

    def reject(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise accounts.AuthError("incorrect username or password")

    monkeypatch.setattr(accounts, "login", reject)
    for _ in range(10):
        assert client.post("/login", data={"username": "john", "password": "wrong"}).status_code == 401

    blocked = client.post("/login", data={"username": "john", "password": "wrong"})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]
    assert calls == 10


def test_account_creation_is_rate_limited(app, monkeypatch):
    application, _ = app
    client = anonymous(application)
    calls = 0

    def reject(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise accounts.AuthError("invalid account")

    monkeypatch.setattr(accounts, "redeem_join_code", reject)
    attempt = {"username": "newcomer", "password": "a-good-password", "join_code": "WRONG-CODE"}
    for _ in range(10):
        assert client.post("/signup", data=attempt).status_code == 400

    blocked = client.post("/signup", data=attempt)
    assert blocked.status_code == 429
    assert calls == 10


# ---------- signing up ----------


def test_anyone_may_enrol_as_a_reviewer_when_self_signup_is_on(app):
    application, db_path = app
    with db.connect(db_path) as conn:
        conn.execute("UPDATE site_settings SET reviewer_self_signup = 1")
    client = anonymous(application)

    response = client.post("/signup", data={
        "username": "newcomer", "password": "a-good-password", "display_name": "Newcomer",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert client.get("/api/me").json()["role"] == "reviewer"


def test_nobody_enrols_without_a_join_code_while_self_signup_is_off(app):
    application, _ = app

    response = anonymous(application).post("/signup", data={
        "username": "newcomer", "password": "a-good-password", "display_name": "Newcomer",
    }, follow_redirects=False)

    assert response.status_code == 400


def test_an_admin_join_code_is_what_makes_an_admin(app):
    application, db_path = app
    with db.connect(db_path) as conn:
        owner_id = accounts.create_owner(conn, "olive", "olive-password")
        code = accounts.create_join_code(conn, accounts.get_user(conn, owner_id), "admin")

    client = anonymous(application)
    right = client.post("/signup", data={
        "username": "louie", "password": "a-good-password", "display_name": "Louie",
        "join_code": code,
    }, follow_redirects=False)
    assert right.status_code == 303
    assert client.get("/api/me").json()["role"] == "admin"


# ---------- what a reviewer may not do ----------


@pytest.mark.parametrize("path", ["/api/team/roster", "/api/team/roster.csv", "/api/settings"])
def test_admin_only_paths_are_refused_to_reviewers(app, path):
    application, _ = app
    response = signed_in(application, "john", "john-password").get(path)

    assert response.status_code == 403
    assert "admins only" in response.json()["detail"]


def test_an_admin_is_refused_none_of_it(app):
    application, _ = app
    client = signed_in(application, "paul", "paul-password")

    assert client.get("/api/team/roster").status_code == 200
    assert client.get("/api/team/roster.csv").status_code == 200
    assert client.get("/api/settings").status_code == 200


# ---------- the workflow, over HTTP ----------


def test_the_full_round_trip_through_the_api(app):
    """Paul assigns, John submits, Paul returns with notes, John resubmits, the accepted folder
    approves itself -- and John cannot shortcut any of it."""
    application, db_path = app
    # Two accepted documents: submitting and approving are gated on the documents now, so a folder
    # has to have something to be finished about.
    seed_folder(db_path, "E2002")
    paul = signed_in(application, "paul", "paul-password")
    john = signed_in(application, "john", "john-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]

    assert paul.post("/api/team/assign", json={
        "folder_id": "E2002", "workflow": "new", "reviewer_id": john_id
    }).status_code == 200

    mine = john.get("/api/my/folders").json()["folders"]
    assert [f["folder_id"] for f in mine] == ["E2002"]
    assert mine[0]["status"] == "assigned"

    assert john.post("/api/my/submit", json={"folder_id": "E2002", "note": "first pass"}).status_code == 200
    assert paul.post("/api/team/review", json={
        "folder_id": "E2002", "status": "returned", "note": "check the dates"
    }).status_code == 200
    assert john.post("/api/my/submit", json={"folder_id": "E2002", "note": "fixed"}).status_code == 200

    # Both documents were accepted before each submit, so each submit approves the folder itself.
    story = [e["action"] for e in paul.get("/api/team/history", params={"folder_id": "E2002"}).json()["history"]]
    assert story == ["assigned", "submitted", "approved", "returned", "submitted", "approved"]


def test_a_reviewer_cannot_approve_their_own_work_over_http(app):
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    john = signed_in(application, "john", "john-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002")
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})

    assert john.post("/api/team/review", json={
        "folder_id": "E2002", "status": "approved"
    }).status_code == 403


def test_one_reviewer_cannot_see_or_submit_anothers_folder(app):
    """The isolation the whole design rests on, exercised through the API rather than the helper."""
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002")
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})

    jack = signed_in(application, "jack", "jack-password")

    assert jack.get("/api/my/folders").json()["folders"] == []
    assert jack.post("/api/my/submit", json={"folder_id": "E2002"}).status_code == 403


def test_my_folders_is_scoped_by_the_session_not_by_a_parameter(app):
    """There is no id for a reviewer to change."""
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002")
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})

    jack = signed_in(application, "jack", "jack-password")
    # Every shape of "show me someone else's" a caller might try.
    for params in ({"reviewer_id": john_id}, {"user_id": john_id}, {"folder_id": "E2002"}):
        assert jack.get("/api/my/folders", params=params).json()["folders"] == []


# ---------- the roster CSV ----------


def test_the_roster_csv_is_clean_and_named(app):
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002")
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})

    response = paul.get("/api/team/roster.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "roster.csv" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert lines[0] == "Name,Folder,Assigned By,Date"
    assert lines[1].startswith("John,E2002,Paul,")
    # The date, not a timestamp: this is read by a person deciding who did what and when.
    assert re.fullmatch(r"John,E2002,Paul,\d{4}-\d{2}-\d{2}", lines[1]), lines[1]


def test_the_roster_csv_does_not_repeat_a_folder_count_on_every_row(app):
    """How many folders somebody has is how many rows carry their name."""
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    for folder in ("E2002", "E2226"):
        seed_folder(db_path, folder)
        paul.post("/api/team/assign", json={"folder_id": folder, "reviewer_id": john_id})

    lines = paul.get("/api/team/roster.csv").text.strip().splitlines()

    assert len([l for l in lines[1:] if l.startswith("John,")]) == 2
    assert "Folders" not in lines[0] and "Events" not in lines[0]


# ---------- choosing a folder rather than typing one ----------


def _a_real_folder(db_path, folder_id="E2226", name="E2226_Lovell Mfg. Company"):
    """A batch plus its documents, the way an upload leaves things."""
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, uploaded_folder_name) "
            "VALUES ('B1', 'now', 4, ?, ?)", (folder_id, name),
        )
        for suffix in ("AAA", "AAB"):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status) VALUES (?, 'B1', 'drafted')",
                (f"{folder_id}{suffix}",),
            )


def test_the_folder_list_offers_what_exists_with_who_holds_it(app):
    """An admin should see the whole board -- assigned and unassigned -- not a memory test."""
    application, db_path = app
    _a_real_folder(db_path)
    paul = signed_in(application, "paul", "paul-password")

    folders = paul.get("/api/team/folders").json()["folders"]

    by_id = {f["folder_id"]: f for f in folders}
    assert "E2226" in by_id
    assert by_id["E2226"]["label"] == "E2226_Lovell Mfg. Company"   # the readable name
    assert by_id["E2226"]["workflow"] == "new"
    assert by_id["E2226"]["assignment_status"] == "unassigned"


def test_an_assigned_folder_still_appears_with_its_holder(app):
    """Reassigning is normal -- someone is away, or the work moved."""
    application, db_path = app
    _a_real_folder(db_path)
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2226")
    paul.post("/api/team/assign", json={"folder_id": "E2226", "workflow": "new", "reviewer_id": john_id})

    folder = next(f for f in paul.get("/api/team/folders").json()["folders"] if f["folder_id"] == "E2226")

    assert folder["assignment_status"] == "assigned"
    assert folder["assigned_to_name"] == "John"


def test_assigning_a_folder_that_does_not_exist_is_refused(app):
    """The bug this closes, exactly as it happened: "E2226_Lovell Mfg."""
    application, db_path = app
    _a_real_folder(db_path)
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]

    response = paul.post("/api/team/assign", json={
        "folder_id": "E2226_Lovell Mfg. Company", "workflow": "new", "reviewer_id": john_id,
    })

    assert response.status_code == 400
    assert "no such folder" in response.json()["detail"]
    with db.connect(db_path) as conn:
        assert accounts.folders_for_reviewer(conn, john_id) == []


def test_assigning_the_right_id_works_and_the_reviewer_can_see_it(app):
    """The other half: the correct id reaches the reviewer's dashboard AND passes the visibility
    check the review queue uses, which is what was silently failing before."""
    application, db_path = app
    _a_real_folder(db_path)
    paul = signed_in(application, "paul", "paul-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]

    assert paul.post("/api/team/assign", json={
        "folder_id": "E2226", "workflow": "new", "reviewer_id": john_id,
    }).status_code == 200

    john = signed_in(application, "john", "john-password")
    assert [f["folder_id"] for f in john.get("/api/my/folders").json()["folders"]] == ["E2226"]
    # And the folder browser -- the thing the Review Queue renders -- now shows it to her.
    assert "E2226" in john.get("/api/new/folders").text


def test_a_folder_uploaded_but_not_yet_split_can_still_be_assigned(app):
    """Those are exactly the folders an admin most wants to hand out: freshly uploaded, nobody on
    them yet."""
    application, db_path = app
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, uploaded_folder_name) "
            "VALUES ('B9', 'now', 70, 'E2300', 'E2300_Fresh Upload')"
        )
    paul = signed_in(application, "paul", "paul-password")

    ids = [f["folder_id"] for f in paul.get("/api/team/folders").json()["folders"]]

    assert "E2300" in ids


def test_a_reviewer_cannot_list_the_folders_available_to_assign(app):
    """It is under /api/team, so the gate covers it -- but worth pinning, since this endpoint's
    whole purpose is naming folders a reviewer is not meant to know about."""
    application, _ = app
    assert signed_in(application, "john", "john-password").get("/api/team/folders").status_code == 403


# ---------- closing your own account ----------


def test_a_reviewer_can_close_their_own_account(app):
    application, db_path = app
    john = signed_in(application, "john", "john-password")

    assert john.post("/api/me/close").status_code == 200

    with db.connect(db_path) as conn:
        with pytest.raises(accounts.AuthError):
            accounts.login(conn, "john", "john-password")


def test_closing_an_account_ends_the_session_immediately(app):
    """Not at whatever hour the cookie happens to expire."""
    application, _ = app
    john = signed_in(application, "john", "john-password")
    john.post("/api/me/close")

    assert john.get("/api/me").status_code == 401


def test_closing_an_account_keeps_the_history_it_is_credited_with(app):
    """Deactivation rather than deletion: an edition whose changelog forgets who did the work is
    worth less than one that remembers."""
    application, db_path = app
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
        paul_id = accounts.get_user_by_name(conn, "paul")["id"]
        accounts.assign_folder(conn, "E2002", "new", john_id, paul_id)

    signed_in(application, "john", "john-password").post("/api/me/close")

    with db.connect(db_path) as conn:
        assert accounts.get_user(conn, john_id)["display_name"] == "John"
        assert [e["by"] for e in accounts.folder_history(conn, "E2002", "new")] == ["Paul"]


def test_the_owner_cannot_close_the_site_out_from_under_everyone(app):
    application, db_path = app
    with db.connect(db_path) as conn:
        accounts.create_owner(conn, "olive", "olive-password")
    olive = signed_in(application, "olive", "olive-password")

    response = olive.post("/api/me/close")

    assert response.status_code == 403
    assert "transfer ownership first" in response.json()["detail"]


def test_an_admin_may_leave_once_someone_else_can_administer(app):
    application, db_path = app
    with db.connect(db_path) as conn:
        accounts.create_user(conn, "ruth", "ruth-password", "Ruth", "admin")

    assert signed_in(application, "paul", "paul-password").post("/api/me/close").status_code == 200


def test_closing_an_account_is_not_blocked_as_a_destructive_verb(app):
    """DELETE is admin-only by verb, which is why this route is a POST."""
    assert webauth.admin_capability("/api/me/close", "POST") is None


def test_someone_can_change_their_own_password(app):
    application, db_path = app
    john = signed_in(application, "john", "john-password")

    assert john.post("/api/me/password", json={
        "current": "john-password", "new": "a-longer-new-password"}).status_code == 200

    with db.connect(db_path) as conn:
        assert accounts.login(conn, "john", "a-longer-new-password")


def test_changing_a_password_requires_the_current_one(app):
    """A browser left open on a shared machine must not be enough to take the account over."""
    application, db_path = app
    john = signed_in(application, "john", "john-password")

    assert john.post("/api/me/password", json={
        "current": "guessing", "new": "a-longer-new-password"}).status_code == 400

    with db.connect(db_path) as conn:
        assert accounts.login(conn, "john", "john-password")


def test_a_password_change_ends_the_sessions_it_was_good_for(app):
    application, _ = app
    john = signed_in(application, "john", "john-password")
    john.post("/api/me/password", json={"current": "john-password", "new": "a-longer-new-password"})

    assert john.get("/api/me").status_code == 401


def test_a_password_too_short_to_be_accepted_is_refused(app):
    application, db_path = app
    john = signed_in(application, "john", "john-password")

    assert john.post("/api/me/password", json={"current": "john-password", "new": "abc"}).status_code == 400

    with db.connect(db_path) as conn:
        assert accounts.login(conn, "john", "john-password")


# ---------- sending a folder back names the documents that need work ----------


def test_returning_a_folder_flags_the_documents_the_admin_picked(app):
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    john = signed_in(application, "john", "john-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002", n=3)
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})
    john.post("/api/my/submit", json={"folder_id": "E2002"})

    assert paul.post("/api/team/review", json={
        "folder_id": "E2002", "status": "returned", "note": "the dates are off",
        "rework_documents": ["E2002AAB"],
    }).status_code == 200

    with db.connect(db_path) as conn:
        statuses = dict(conn.execute(
            "SELECT document_id, review_status FROM new_documents").fetchall())
    assert statuses["E2002AAB"] == "needs_work"
    assert statuses["E2002AAA"] == "accepted"


def test_a_returned_folder_without_a_pick_flags_nothing(app):
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    john = signed_in(application, "john", "john-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002", n=2)
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})
    john.post("/api/my/submit", json={"folder_id": "E2002"})

    paul.post("/api/team/review", json={
        "folder_id": "E2002", "status": "returned", "note": "one more pass please"})

    with db.connect(db_path) as conn:
        flagged = conn.execute(
            "SELECT COUNT(*) FROM new_documents WHERE review_status = 'flagged'").fetchone()[0]
    assert flagged == 0


def test_a_reviewer_cannot_flag_documents_by_returning_a_folder(app):
    """`rework_documents` rides on the admin-only return, so the whole call is refused."""
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    john = signed_in(application, "john", "john-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002", n=2)
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})

    assert john.post("/api/team/review", json={
        "folder_id": "E2002", "status": "returned", "rework_documents": ["E2002AAA"],
    }).status_code == 403
    with db.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM new_documents WHERE review_status = 'flagged'").fetchone()[0] == 0


def test_a_pick_naming_another_folders_document_is_ignored(app):
    application, db_path = app
    paul = signed_in(application, "paul", "paul-password")
    john = signed_in(application, "john", "john-password")
    with db.connect(db_path) as conn:
        john_id = accounts.get_user_by_name(conn, "john")["id"]
    seed_folder(db_path, "E2002", n=2)
    seed_folder(db_path, "E2003", n=2)
    paul.post("/api/team/assign", json={"folder_id": "E2002", "reviewer_id": john_id})
    john.post("/api/my/submit", json={"folder_id": "E2002"})

    paul.post("/api/team/review", json={
        "folder_id": "E2002", "status": "returned", "note": "see the flags",
        "rework_documents": ["E2003AAA", 7, None]})

    with db.connect(db_path) as conn:
        assert conn.execute("SELECT review_status FROM new_documents "
                            "WHERE document_id = 'E2003AAA'").fetchone()[0] == "accepted"
