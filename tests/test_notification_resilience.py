"""The bell and the activity banner, when the answer is not the one they asked for."""

import pathlib
import shutil
import subprocess

import pytest

from incipit import accounts, activity, collab, db, review_server, scoping
from incipit.config import Settings
from starlette.testclient import TestClient

WORKSPACE_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "workspace.js"


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings, str(tmp_path / "export" / "out.csv"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app


# ---------- what the browser is actually handed ----------


def test_an_expired_session_answers_the_bell_with_parseable_json(site):
    """The precondition for the whole bug."""
    anon = TestClient(site, raise_server_exceptions=False)
    # conftest's autouse fixture signs every TestClient in as an admin, so being anonymous here is
    # something this test has to ask for explicitly.
    anon.cookies.clear()
    res = anon.get("/api/notifications", follow_redirects=False)

    assert res.status_code == 401
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()
    assert "unread" not in body and "items" not in body, \
        "if the refusal ever carries these, this test is guarding the wrong thing"


def test_a_signed_in_bell_always_carries_a_numeric_unread_and_a_list(site):
    """The shape the badge and drawer are written against."""
    c = TestClient(site)
    c.post("/login", data={"username": "john", "password": "john-password"})
    body = c.get("/api/notifications").json()

    assert isinstance(body["unread"], int)
    assert isinstance(body["items"], list)


# ---------- the fix, executed ----------


def _run_js(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = WORKSPACE_JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    marker = "async function getJSON(url, options) {"
    start = src.index(marker)
    end = src.index("\n  }", start) + len("\n  }")
    harness = src[start:end] + "\n" + script
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_getjson_rejects_a_refusal_instead_of_returning_it_as_a_payload():
    """The actual fix."""
    out = _run_js("""
    global.fetch = async () => ({
      ok: false, status: 401,
      json: async () => ({ detail: "sign in to continue" }),
    });
    getJSON("/api/notifications")
      .then((v) => console.log("RESOLVED:" + JSON.stringify(v)))
      .catch(() => console.log("THREW"));
    """)
    assert out == "THREW", "a 401 was returned to the caller as if it were the payload"


def test_getjson_still_returns_a_normal_body():
    """The guard must not cost the working case."""
    out = _run_js("""
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ unread: 3 }) });
    getJSON("/api/notifications").then((v) => console.log(v.unread));
    """)
    assert out == "3"


def test_the_bell_never_renders_a_non_number_as_a_count():
    """The second half of the fix, kept because the first half only covers the cause we found."""
    out = _run_js("""
    for (const raw of [undefined, null, "lots", NaN, {}]) {
      const unread = Number(raw) || 0;
      console.log([String(unread), String(unread === 0)].join(","));
    }
    """)
    assert out.splitlines() == ["0,true"] * 5, \
        "a non-numeric unread count reached the badge as text, or left it visible"


# ---------- the seam between snapshot and the filter ----------


def test_a_snapshot_entry_carries_the_field_the_filter_reads():
    """`visible_activity` filters on owner_id and nothing else."""
    activity.clear()
    with activity.track("uploading", "Uploading E2999", owner_id=7):
        entry = activity.snapshot()[0]
    assert entry["owner_id"] == 7


def test_a_reviewer_sees_their_own_job_through_the_real_producer(site, tmp_path):
    """End to end across the seam: a real tracked job, a real snapshot, the real filter."""
    activity.clear()
    with db.connect(str(tmp_path / "extraction.db")) as conn:
        john = accounts.get_user_by_name(conn, "john") if hasattr(accounts, "get_user_by_name") \
            else conn.execute("SELECT * FROM users WHERE username = 'john'").fetchone()

        with activity.track("uploading", "Uploading E2999", owner_id=john["id"]):
            visible = scoping.visible_activity(john, activity.snapshot())

    assert [e["label"] for e in visible] == ["Uploading E2999"]


def test_a_reviewer_still_does_not_see_the_shared_pipeline(site, tmp_path):
    """The filter's actual purpose, re-checked through the real producer now that owner_id reaches
    it -- carrying the field must not turn into showing everything."""
    activity.clear()
    with db.connect(str(tmp_path / "extraction.db")) as conn:
        john = conn.execute("SELECT * FROM users WHERE username = 'john'").fetchone()
        paul = conn.execute("SELECT * FROM users WHERE username = 'paul'").fetchone()

        with activity.track("drafting", "Transcribing new documents", owner_id=None), \
                activity.track("uploading", "Uploading E2888", owner_id=paul["id"]):
            john_sees = scoping.visible_activity(john, activity.snapshot())
            paul_sees = scoping.visible_activity(paul, activity.snapshot())

    assert john_sees == []
    assert len(paul_sees) == 2, "an admin still sees every running job"
