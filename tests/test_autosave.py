"""Edits that keep themselves, without burying the history that describes them."""
import pathlib
import re

import pytest
from starlette.testclient import TestClient

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from conftest import insert_rows

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
AUTOSAVE = (STATIC / "autosave.js").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        # A second editor, because two people editing one document is the case coalescing must
        # not blur together.
        accounts.create_user(conn, "mary", "mary-password", "Mary", "admin")
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B', 'now', 1, 'D1')")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, text) "
                     "VALUES ('D1AAA', 'B', 'drafted', 'the original text')")
    client = TestClient(application)
    client.cookies.clear()
    client.post("/login", data={"username": "paul", "password": "paul-password"},
                follow_redirects=False)
    return client, db_path


def save(client, text, status="unreviewed"):
    return client.post("/api/new/documents/D1AAA/review", json={
        "reviewed_text": text, "marginal_notes": [], "places": [], "names": [],
        "challenges": [], "review_status": status, "editors_notes": ""})


def revisions(db_path, field="reviewed_text"):
    with db.connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT previous, current, author_id FROM text_revisions WHERE field = ? "
            "ORDER BY id", (field,))]


# ---------- what autosave would otherwise do to the history ----------


def test_one_sitting_of_edits_is_one_history_entry(site):
    """Autosave writes every few seconds, so without this a single corrected word would arrive as
    a row per pause -- and the history stops being something a reader can use."""
    client, db_path = site

    save(client, "the original text with one")
    save(client, "the original text with one word")
    save(client, "the original text with one word changed")

    assert len(revisions(db_path)) == 1


def test_that_entry_spans_from_where_the_edit_started_to_where_it_ended(site):
    client, db_path = site

    save(client, "the original text with one")
    save(client, "the original text with one word changed")

    entry = revisions(db_path)[0]
    assert entry["previous"] == "the original text"
    assert entry["current"] == "the original text with one word changed"


def test_editing_back_to_the_original_leaves_no_entry_at_all(site):
    """Typing a word and taking it out again is not an edit anyone needs told about."""
    client, db_path = site

    save(client, "the original text plus a mistake")
    save(client, "the original text")

    assert revisions(db_path) == []


def test_a_later_sitting_is_its_own_entry(site):
    """Coming back to the same field an hour later is a second edit, not a continuation."""
    client, db_path = site
    save(client, "the original text, once")
    with db.connect(db_path) as conn:
        conn.execute("UPDATE text_revisions SET created_at = '2020-01-01T00:00:00+00:00'")

    save(client, "the original text, once and again")

    assert len(revisions(db_path)) == 2


def test_another_person_s_edit_is_never_folded_into_yours(site):
    """The history's whole job is saying who changed what."""
    client, db_path = site
    save(client, "the original text, edited by Paul")
    mary = TestClient(client.app)
    mary.cookies.clear()
    mary.post("/login", data={"username": "mary", "password": "mary-password"},
              follow_redirects=False)

    assert save(mary, "the original text, edited by Paul then Mary").status_code == 200
    assert len({entry["author_id"] for entry in revisions(db_path)}) == 2


def test_the_coalescing_window_is_long_enough_to_cover_a_save_cycle():
    """It has to outlast the gap autosave leaves between writes, or every write starts an entry."""
    ceiling = int(re.search(r"MAX_INTERVAL_MS = (\d+)", AUTOSAVE).group(1))

    assert collab.REVISION_COALESCE_SECONDS * 1000 > ceiling


# ---------- what the module is allowed to do ----------


def test_autosave_cannot_set_a_review_status():
    """A reviewer must never find work recorded as accepted that they did not accept, so the hook
    saves at whatever status the document already has and offers no way to pass one."""
    hook = APP[APP.index("window.__draft__ = {"):]
    hook = hook[:hook.index("};") + 2]

    assert "state.currentDoc.review.status" in hook
    assert "accepted" not in hook and "flagged" not in hook


def test_autosave_reaches_app_js_only_through_that_hook():
    """Its own module with its own job: it must not be reading app.js's state behind its back."""
    assert "window.__draft__" in AUTOSAVE
    for forbidden in ("state.", "currentDoc.", "textEdit", "fetch("):
        assert forbidden not in AUTOSAVE, f"autosave.js reaches past the hook: {forbidden}"


def test_a_quiet_save_does_not_redraw_the_screen_under_the_reviewer():
    """The new-documents save refetches and re-renders to pick up the recomputed title, which
    would move the caret out of the field being typed into."""
    assert "isNew && !quiet" in APP


def test_a_quiet_save_says_nothing_in_the_toast():
    assert "if (quiet) return true;" in APP


def test_an_explicit_verdict_waits_for_autosave_instead_of_dropping_the_click():
    """Accept/Mark Unreviewed is a decision, and may be clicked during a background write."""
    start = APP.index("async function saveReview(")
    body = APP[start:APP.index('el("btn-save-draft")', start)]

    assert "await state.savePromise" in body
    assert "return saveReview(status, triggerButton, { quiet });" in body


def test_the_page_loads_the_module():
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "autosave.js" in index
    assert index.index("app.js") < index.index("autosave.js"), "the hook is published by app.js"


def test_a_save_only_clears_the_dirty_flag_for_the_payload_it_sent():
    """Reported in review: words typed while a request was in flight were marked saved, so the
    next autosave saw nothing to do and leaving the page did not warn."""
    body = APP[APP.index("async function saveReview("):]
    body = body[:body.index("\n  el(\"btn-save-draft\")")]

    assert "const generation = state.editGeneration" in body
    assert "=== generation) state.dirty = false" in body


def test_every_edit_moves_the_generation_on():
    marker = APP[APP.index("function markDirty()"):]
    marker = marker[:marker.index("\n  }") + 4]

    assert "state.editGeneration" in marker
