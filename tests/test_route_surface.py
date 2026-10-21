"""Every route, checked as a whole rather than one at a time."""

import pytest

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from starlette.testclient import TestClient
from conftest import insert_rows

ANONYMOUS_IS_REFUSED = (401, 403, 302, 303, 307)
# Public by design (M2): what the sign-in page draws, and owner creation, which needs the token.
PUBLIC_API = {"/api/site", "/api/setup/owner"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    # Its own status directory: the default is the repository's real deploy/logs, so /health was
    # answering from whenever this workstation last ran a backup.
    application = review_server.create_app(settings, str(tmp_path / "export" / "out.csv"),
        str(tmp_path / "logs"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return application


def client(app, who=None):
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.clear()
    if who:
        c.post("/login", data={"username": who, "password": f"{who}-password"},
               follow_redirects=False)
    return c


def api_routes(app):
    """(path, method) for every /api route, with path params filled in with harmless values."""
    out = set()
    for route in app.routes:
        if not hasattr(route, "methods") or not str(route.path).startswith("/api"):
            continue
        concrete = (str(route.path)
                    .replace("{document_id}", "X")
                    .replace("{batch_id}", "B")
                    .replace("{comment_id}", "1")
                    .replace("{sequence_index}", "0")
                    .replace("{asset_id}", "1")
                    .replace("{user_id}", "1")
                    .replace("{code_id}", "1")
                    .replace("{step}", "site"))
        for method in route.methods:
            if method not in ("HEAD", "OPTIONS"):
                out.add((concrete, method))
    return sorted(out)


def test_there_are_routes_to_check(app):
    """A guard on the guard: if the walk stopped finding routes, every assertion below would pass
    against an empty set and report nothing."""
    assert len(api_routes(app)) > 40


def test_no_api_route_is_reachable_without_signing_in(app):
    """The gate is one middleware over everything, so what has to be tested is that nothing escapes
    it -- not that each endpoint remembered to check."""
    anon = client(app)
    reachable = [
        f"{method} {path} -> {anon.request(method, path, json={}, follow_redirects=False).status_code}"
        for path, method in api_routes(app)
        if path not in PUBLIC_API
        and anon.request(method, path, json={}, follow_redirects=False).status_code
        not in ANONYMOUS_IS_REFUSED
    ]
    assert reachable == [], f"reachable without a session: {reachable}"


@pytest.mark.parametrize("who", ["john", "paul"])
def test_no_route_returns_a_server_error_for_a_malformed_request(app, who):
    """An empty JSON body against every endpoint."""
    c = client(app, who)
    crashed = [
        f"{method} {path} -> {c.request(method, path, json={}, follow_redirects=False).status_code}"
        for path, method in api_routes(app)
        if c.request(method, path, json={}, follow_redirects=False).status_code >= 500
    ]
    assert crashed == [], f"5xx on a malformed body: {crashed}"


def test_the_login_and_signup_pages_stay_public(app):
    """The gate must not lock out the pages used to get through it."""
    anon = client(app)
    assert anon.get("/login").status_code == 200
    assert anon.get("/style.css").status_code in (200, 404)   # static, never gated


def test_an_unknown_path_is_not_a_way_in(app):
    """A path the router does not know must still meet the gate rather than falling through it."""
    anon = client(app)
    assert anon.get("/api/definitely-not-a-route",
                    follow_redirects=False).status_code in ANONYMOUS_IS_REFUSED


# ---------- under concurrency ----------


def test_simultaneous_claims_leave_exactly_one_holder(app):
    """Starlette runs sync handlers on a threadpool, so two editors clicking Claim at the same
    moment genuinely execute together."""
    import concurrent.futures as futures

    editors = [client(app, "paul"), client(app, "paul"), client(app, "paul")]
    with futures.ThreadPoolExecutor(max_workers=len(editors)) as pool:
        codes = list(pool.map(
            lambda c: c.post("/api/team/claim",
                             json={"folder_id": "E1", "workflow": "new"}).status_code, editors))

    assert set(codes) == {200}
    assert len(client(app, "paul").get("/api/team/claims").json()["claims"]) == 1


def test_the_two_roles_stay_separate_under_concurrent_writes(app, tmp_path):
    """The separation is only worth having if it survives the case it was built for: a reviewer and
    an admin judging the same document at the same time."""
    import concurrent.futures as futures

    with db.connect(str(tmp_path / "extraction.db")) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B', 'now', 1, 'E1')")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status, text)"
                     " VALUES ('E1AAA', 'B', 'drafted', 'unreviewed', 't')")
        # The reviewer has to hold the folder to see its documents at all.
        accounts.assign_folder(conn, "E1", "new", accounts.get_user_by_name(conn, "john")["id"],
                               accounts.get_user_by_name(conn, "paul")["id"])

    def review(who, status):
        return client(app, who).post(
            "/api/new/documents/E1AAA/review",
            json={"review_status": status, "reviewer": "", "editors_notes": "",
                  "reviewed_text": "t"}).status_code

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(review, "paul", "accepted") for _ in range(4)]
        # Flag is no longer a reviewer verdict; taking an acceptance back to unreviewed is the
        # current second decision path and still must remain separate from the admin's acceptance.
        jobs += [pool.submit(review, "john", "unreviewed") for _ in range(4)]
        assert {j.result() for j in jobs} == {200}

    def seen_by(who):
        return [d["review_status"] for d in client(app, who).get("/api/new/documents").json()
                if d["document_id"] == "E1AAA"]

    assert seen_by("paul") == ["accepted"]
    assert seen_by("john") == ["unreviewed"], "the roles' verdicts merged under concurrency"


@pytest.mark.parametrize("hostile", [
    "../../etc/passwd", "'; DROP TABLE new_documents;--", "<script>alert(1)</script>",
    "A" * 5000, "%s%n", "../" * 50,
])
def test_hostile_folder_ids_are_refused_rather_than_fatal(app, hostile):
    """A folder id reaches SQL as a bound parameter, a filename in a header, and a path prefix in
    the bucket."""
    paul = client(app, "paul")
    for path, payload in (
        ("/api/team/claim", {"folder_id": hostile, "workflow": "new"}),
        ("/api/my/submit", {"folder_id": hostile, "workflow": "new"}),
        ("/api/team/review", {"folder_id": hostile, "workflow": "new", "status": "approved"}),
    ):
        assert paul.post(path, json=payload).status_code < 500, f"{path} crashed on {hostile[:24]!r}"


# ---------- listing folders is one pass, not one per folder ----------


def test_listing_folders_scans_the_documents_once(tmp_path):
    """filter_folders called folder_progress inside its loop, and each call scanned the whole
    document table -- F folders read D documents F times."""
    import sqlite3

    from incipit import scoping

    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        for f in range(8):
            insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                         "VALUES (?, 'now', 1, ?)", (f"B{f}", f"E{f:03}"))
            for d in range(10):
                insert_rows(conn,
                    "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                    "VALUES (?, ?, 'drafted', 'unreviewed')",
                    (f"E{f:03}A{chr(65 + d)}A", f"B{f}"))
        folders = db.get_new_document_folders(conn)

    scans = []

    class Counting(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "FROM new_documents" in sql:
                scans.append(sql)
            return super().execute(sql, *args, **kwargs)

    conn = sqlite3.connect(db_path, factory=Counting)
    conn.row_factory = sqlite3.Row
    try:
        listed = scoping.filter_folders(conn, {"id": 0, "role": "admin"}, folders, "new")
    finally:
        conn.close()

    assert len(listed) == 8
    assert listed[0]["progress"]["total"] == 10, "the single pass changed the counts"
    assert len(scans) == 1, f"{len(scans)} scans for 8 folders -- the per-folder scan is back"


def test_a_missing_viewer_is_shown_no_folder_at_all(tmp_path):
    """Fail closed (M2 AC-3): code that lost track of who is asking returns nothing, not everything."""
    from incipit import scoping

    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        assert scoping.visible_folder_ids(conn, None, "new") == set()
        assert scoping.filter_folders(conn, None, [], "new") == []


# ---------- /health ----------


def test_health_is_reachable_without_signing_in():
    """A monitor has no credentials."""
    from incipit import webauth
    assert webauth.is_public("/health")


def test_health_reports_ok_when_the_database_answers(app):
    r = client(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] is True


def test_health_is_503_when_the_database_cannot_be_reached(app, monkeypatch):
    """The case worth catching: the site answers, the screens render, and every save fails."""
    from incipit import system_routes

    monkeypatch.setattr(system_routes, "_database_reachable", lambda path: False)
    r = client(app).get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["database"] is False


def test_health_names_the_commit_being_served(app):
    """So a monitor can tell a restart from a redeploy."""
    assert "commit" in client(app).get("/health").json()


def test_health_leaks_nothing(app):
    """Public and unauthenticated, so it says whether things work and never what they contain -- no
    counts, no paths, nothing naming a person or a machine."""
    body = client(app).get("/health").json()
    assert set(body) == {"status", "commit", "database", "backup"}
    flat = str(body).lower()
    for leak in ("users", "c:\\", "/home/", "extraction.db", "openrouter", "token"):
        assert leak not in flat, f"/health exposed {leak!r}"


def test_a_backup_that_has_stopped_makes_the_deployment_degraded(app, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from incipit import system_routes

    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    monkeypatch.setattr(system_routes.backup, "read_status",
                        lambda d: {"ok": True, "last_success_at": stale})
    r = client(app).get("/health")
    assert r.status_code == 503
    assert r.json()["backup"] > system_routes.STALE_BACKUP_SECONDS


def test_a_recent_backup_keeps_it_healthy(app, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from incipit import system_routes

    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    monkeypatch.setattr(system_routes.backup, "read_status",
                        lambda d: {"ok": True, "last_success_at": fresh})
    assert client(app).get("/health").status_code == 200


def test_no_backup_configured_is_not_reported_as_a_stopped_backup(app, monkeypatch):
    """"No backups here" and "backups have stopped" need different answers from whoever is paged."""
    from incipit import system_routes

    monkeypatch.setattr(system_routes.backup, "read_status", lambda d: None)
    r = client(app).get("/health")
    assert r.status_code == 200
    assert r.json()["backup"] is None
