"""Shared test fixtures and helpers."""

import contextlib
import functools
import io

import pytest
from PIL import Image

# Every test's folders live under this workflow until tabs arrive (M4).
DEFAULT_TAB = "new"


def tiny_image_bytes(
    format: str = "JPEG", size: tuple[int, int] = (2, 2), color: tuple[int, int, int] = (200, 200, 200)
) -> bytes:
    """A real, minimal, Pillow-decodable image: ingest runs every upload through
    imaging.normalize_image_bytes, which rejects anything Pillow cannot open."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format=format)
    return buffer.getvalue()


_LARGE_JPEG_BYTES: bytes | None = None


def large_jpeg_bytes() -> bytes:
    """A real ~700 KB JPEG (noise, so it does not compress away) for split-window byte budgets."""
    global _LARGE_JPEG_BYTES
    if _LARGE_JPEG_BYTES is None:
        buffer = io.BytesIO()
        Image.effect_noise((1300, 950), 40).convert("RGB").save(buffer, format="JPEG", quality=85)
        _LARGE_JPEG_BYTES = buffer.getvalue()
    return _LARGE_JPEG_BYTES


@pytest.fixture(autouse=True)
def own_location_file(tmp_path_factory, monkeypatch):
    """No test reads or writes this computer's real record of where its data folder is."""
    monkeypatch.setenv("INCIPIT_SETTINGS_DIR", str(tmp_path_factory.mktemp("settings")))


@pytest.fixture(autouse=True)
def no_https_listener_carries_over(monkeypatch):
    """Whether HTTPS runs is process state; each test starts with none."""
    from incipit import tls
    monkeypatch.setattr(tls, "serving", {"port": 0, "error": ""})


@pytest.fixture(autouse=True)
def no_real_tunnel(monkeypatch, tmp_path_factory):
    """No test opens this computer to the internet through a real cloudflared."""
    monkeypatch.setenv("INCIPIT_CLOUDFLARED", str(tmp_path_factory.getbasetemp() / "no-cloudflared.exe"))


@pytest.fixture(autouse=True)
def no_update_carries_over(monkeypatch):
    """What the last update check found is process state; each test starts unchecked, with no feed."""
    from incipit import updates
    monkeypatch.setattr(updates, "_state", {"state": "unchecked", "checked_at": None, "release": None, "error": "",
                                            "downloaded": None})
    monkeypatch.delenv(updates.ENV_FEED, raising=False)


@pytest.fixture(autouse=True)
def no_real_team_page(monkeypatch):
    """No test writes a public Telegraph page or asks public DNS; both point at a closed local port."""
    from incipit import access_routes, fixedlink
    monkeypatch.setattr(fixedlink, "API", "http://127.0.0.1:9/telegraph")
    monkeypatch.setattr(fixedlink, "DNS_OVER_HTTPS", "http://127.0.0.1:9/dns-query")
    monkeypatch.setattr(access_routes, "_links", {})


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    """No test ever sleeps for a real retry backoff."""
    from incipit import modeling

    monkeypatch.setattr(modeling, "RETRY_BACKOFF_SECONDS", (0, 0, 0))


@functools.lru_cache(maxsize=1)
def _test_admin_hash() -> str:
    from incipit import accounts
    return accounts.hash_password("test-admin-password")


@pytest.fixture(autouse=True)
def signed_in_as_admin(monkeypatch):
    """Every TestClient in this suite arrives already signed in as an admin."""
    from fastapi.testclient import TestClient
    from incipit import accounts, db, webauth

    original_init = TestClient.__init__

    def init_and_sign_in(self, app, *args, **kwargs):
        original_init(self, app, *args, **kwargs)
        settings = getattr(getattr(app, "state", None), "auth_settings", None)
        if settings is None:
            return
        with db.connect(settings.db_path) as conn:
            accounts.init_accounts(conn)
            conn.execute(
                "INSERT OR IGNORE INTO users (username, display_name, role, password_hash, created_at) "
                "VALUES ('testadmin', 'Test Admin', 'admin', ?, datetime('now'))",
                (_test_admin_hash(),),
            )
            user_id = conn.execute("SELECT id FROM users WHERE username='testadmin'").fetchone()[0]
            token = accounts.start_session(conn, user_id)
        self.cookies.set(webauth.SESSION_COOKIE, token)

    monkeypatch.setattr(TestClient, "__init__", init_and_sign_in)


# ---------- one way to build an app and sign people in ----------

def make_app(tmp_path, monkeypatch=None, *, admins=("paul",), reviewers=("john",), owner=None,
             export_dir=None):
    """A review app on a temp database and bucket, with `<name>` / `<name>-password` accounts.

    Returns (app, settings). `owner` names an owner account too; `monkeypatch` is accepted for
    callers that still pass it.
    """
    from incipit import accounts, collab, db, review_server
    from incipit.config import Settings

    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    export_dir = export_dir or str(tmp_path / "exports")
    # Its own status directory: the default would read the repository's own deploy logs.
    app = review_server.create_app(settings, export_dir, str(tmp_path / "logs"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        if owner:
            accounts.create_owner(conn, owner, f"{owner}-password", owner.capitalize())
        for name in admins:
            accounts.create_user(conn, name, f"{name}-password", name.capitalize(), "admin")
        for name in reviewers:
            accounts.create_user(conn, name, f"{name}-password", name.capitalize(), "reviewer")
    return app, settings


def signed_in(app, who="paul", password=None, *, raise_server_exceptions=True):
    """A client signed in as `who` (password `<who>-password` unless given), or anonymous when
    `who` is None. Clears the cookie the autouse admin fixture set first."""
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    client.cookies.clear()
    if who:
        client.post("/login", data={"username": who, "password": password or f"{who}-password",
                                    "next": "/"}, follow_redirects=False)
    return client


@pytest.fixture
def app(tmp_path, monkeypatch):
    """The standard app: paul (admin) and john (reviewer)."""
    application, _ = make_app(tmp_path, monkeypatch)
    return application


@pytest.fixture
def admin_client(app):
    return signed_in(app, "paul")


@pytest.fixture
def reviewer_client(app):
    return signed_in(app, "john")


@pytest.fixture
def anon_client(app):
    return signed_in(app, None, raise_server_exceptions=False)


def unreachable_destination(tmp_path):
    """A backup destination that cannot be created, on any platform."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    return blocker / "Editing" / "AI Backup"


# ---------- seeding folders and documents by hand ----------
# A seeded folder or document uses its display id as its internal id, so a test reads "E2002".


def seed_folder(conn, display_id, *, folder_id=None, tab_id=1, name=""):
    """A folders row (created once), returning its internal id."""
    folder_id = folder_id or display_id
    conn.execute(
        "INSERT OR IGNORE INTO folders (folder_id, tab_id, display_id, name, uploaded_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))", (folder_id, tab_id, display_id, name, name))
    return folder_id


def seed_batch(conn, batch_id, image_count=1, *, folder=None, name="", **kwargs):
    """db.create_batch, in the folder `folder` (seeded on first use) when one is given."""
    from incipit import db

    folder_id = seed_folder(conn, folder, name=name) if folder else None
    db.create_batch(conn, batch_id, image_count, folder_id=folder_id, uploaded_folder_name=name, **kwargs)
    return folder_id


def seed_document(conn, document_id, batch_id, pages=({"sequence_index": 0},), *, folder=None,
                  display_id=None):
    """db.create_new_document with a readable internal id, in its batch's folder unless told."""
    from incipit import db

    if folder is not None:
        folder_id = seed_folder(conn, folder)
    else:
        row = conn.execute("SELECT folder_id FROM new_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        folder_id = row[0] if row is not None and row[0] else seed_folder(conn, "F1")
    return db.create_new_document(conn, folder_id, display_id or document_id, batch_id, list(pages),
                                  document_id=document_id)


def seed_draft(conn, document_id, *, result, ai_metadata=None, model="m", provider="openrouter",
               ocr_draft_text="", output_json=None, cost_estimate=None, duration_seconds=None):
    """A finished draft under the default profile, written from the flat shape a test reads most easily."""
    from incipit import db, fields, profiles
    from incipit.new_document import document_title

    meta = dict(ai_metadata or {})
    values = {k: v for k, v in {**result, **meta}.items() if k in fields.BUILTIN_KEYS}
    if isinstance(values.get("subjects"), str):
        values["subjects"] = [t.strip() for t in values["subjects"].split(";") if t.strip()]
    version_id = profiles.default_version_id(conn)
    row = db.get_new_document(conn, document_id)
    title = meta.get("title") or document_title(profiles.compiled(conn, version_id),
                                                row["display_id"] if row else document_id, values)
    db.upsert_new_document_ok(
        conn, document_id, model=model, provider=provider, prompt_version_id=version_id,
        result={"text": result.get("text", ""), "uncertain_words": result.get("uncertain_words", []),
                "fields": values},
        title=title, output_json=output_json or {}, cost_estimate=cost_estimate,
        duration_seconds=duration_seconds, ocr_draft_text=ocr_draft_text)


REVIEW_KEYS = ("reviewed_text", "title", "review_status", "reviewer", "editors_notes", "draft_version")


def review_payload(document, **changes):
    """The review form's save for a fetched document, with `changes` applied; field keys go under `fields`."""
    payload = {"reviewed_text": document["transcription"]["text"], "title": document["metadata"]["title"],
               "fields": dict(document["fields"]), "review_status": "accepted", "reviewer": "Paul",
               "editors_notes": ""}
    for key, value in changes.items():
        (payload if key in REVIEW_KEYS else payload["fields"])[key] = value
    return payload


def bill(db_path, cost: float, purpose: str | None = None) -> None:
    """Records a billed call in the ledger, as the real client does for every response; for fakes
    that stand in for the whole client, and for tests that need spend already on the books."""
    from incipit import ledger
    purpose = purpose or ledger.current_context().get("purpose", "other")
    ledger._insert(str(db_path), {"purpose": purpose, "cost_usd": cost, "cost_source": "provider", "outcome": "ok"})


def draft_call(messages, settings, **kwargs):
    """openrouter.call_openrouter as a document draft under the default profile asks for it."""
    from incipit import openrouter, prompting

    compiled = prompting.compile({})
    kwargs.setdefault("schema", compiled.document_schema)
    kwargs.setdefault("extract_result", openrouter.document_extractor(compiled.registry))
    return openrouter.call_openrouter(messages, settings, **kwargs)


def adopt_documents(conn):
    """Hand-inserted documents take their batch's folder and their own id as display id."""
    conn.execute("UPDATE new_documents SET folder_id = (SELECT b.folder_id FROM new_batches b "
                 "WHERE b.batch_id = new_documents.batch_id) WHERE folder_id IS NULL")
    conn.execute("UPDATE new_documents SET display_id = document_id WHERE display_id IS NULL")


def insert_rows(conn, sql, params=(), *, many=False):
    """A hand-written INSERT into new_batches or new_documents: a folder a batch names is seeded
    under that id (named by uploaded_folder_name), and documents adopt their batch's folder."""
    conn.execute("PRAGMA defer_foreign_keys = ON")
    cursor = conn.executemany(sql, params) if many else conn.execute(sql, params)
    for folder_id, name in conn.execute(
            "SELECT folder_id, MAX(COALESCE(uploaded_folder_name, '')) FROM new_batches "
            "WHERE folder_id IS NOT NULL GROUP BY folder_id "
            "UNION SELECT DISTINCT folder_id, '' FROM new_documents WHERE folder_id IS NOT NULL").fetchall():
        seed_folder(conn, folder_id, name=name)
    adopt_documents(conn)
    return cursor


def document_named(conn, display_id, folder=None):
    """The internal id of the document shown as `display_id` (in the folder shown as `folder`)."""
    sql = ("SELECT d.document_id FROM new_documents d JOIN folders f ON f.folder_id = d.folder_id "
           "WHERE d.display_id = ?")
    args = [display_id]
    if folder is not None:
        sql += " AND f.display_id = ?"
        args.append(folder)
    rows = conn.execute(sql, args).fetchall()
    assert len(rows) == 1, f"{len(rows)} documents are shown as {display_id!r}"
    return rows[0][0]


@contextlib.contextmanager
def next_document_ids(*names):
    """Documents created inside the block take these internal ids, in order."""
    from unittest import mock

    from incipit import db

    with mock.patch.object(db, "new_document_id", side_effect=list(names)):
        yield


@pytest.fixture
def readable_ids(monkeypatch):
    """New folders and confirmed or re-split documents take their display ids as internal ids."""
    from unittest import mock

    from incipit import db, review_server, split

    real_ensure = db.ensure_folder

    def ensure(conn, display_id, name="", tab_id=1):
        with mock.patch.object(db, "new_folder_id", return_value=display_id):
            return real_ensure(conn, display_id, name, tab_id)

    monkeypatch.setattr(db, "ensure_folder", ensure)

    def named(real):
        def call(target, groups, settings):
            with next_document_ids(*(g["display_id"] for g in groups)):
                return real(target, groups, settings)
        return call

    for name in ("confirm_batch_split", "resplit_documents"):
        wrapped = named(getattr(split, name))
        monkeypatch.setattr(split, name, wrapped)
        if hasattr(review_server, name):
            monkeypatch.setattr(review_server, name, wrapped)


def terms_js() -> str:
    """JavaScript defining `term` from the real terms.js, for node harnesses that run app code."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "terms.js").read_text(encoding="utf-8")
    return ("const term = (function () { const window = {}; const document = { readyState: 'complete', "
            "querySelectorAll: () => [], addEventListener() {} };\n" + source + "\nreturn window.term; })();\n")
