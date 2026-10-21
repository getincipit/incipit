"""Covers M4 (docs/specs/M4-data-driven-tabs.md): tabs made, renamed, paused, reordered and deleted by
administrators; folders, documents, batches, exports and the Team list scoped by tab; the strip, the
hash and Settings → Tabs drawn from /api/tabs."""

import time
from pathlib import Path

import pytest

from incipit import db, folder_exports, new_document, openrouter, profiles, split, tabs
from incipit.new_document import NewDocumentOptions
from conftest import make_app, signed_in, tiny_image_bytes
from test_browser_back_button import _run as run_history

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
REVIEW = {"reviewed_text": "Reviewed.", "title": "", "fields": {}, "review_status": "accepted", "editors_notes": ""}


@pytest.fixture
def site(tmp_path):
    """Olive the owner, Paul an admin, John a reviewer, and a model key so drafting is allowed."""
    app, settings = make_app(tmp_path, owner="olive", admins=("paul",), reviewers=("john",))
    settings.openrouter_api_key = "sk-test"
    return app, settings


@pytest.fixture
def model(monkeypatch):
    """A model that drafts every document the same way."""
    def fake(messages, settings, schema, extract_result, extra=None):
        return extract_result({"text": "Dear Sir,", "uncertain_words": [], "document_type": "Letter",
                               "date": "1901-03-04", "author": "Smith, Jane", "recipient": "", "place": "",
                               "names": [], "subjects": [], "places": [], "marginal_notes": [], "challenges": []},
                              {"cost": 0.001})
    monkeypatch.setattr(openrouter, "call_openrouter", fake)


def _tab(client, label, **body):
    made = client.post("/api/tabs", json={"label": label, **body})
    assert made.status_code == 200, made.text
    return made.json()["tab"]


def _folder(settings, tab_id, display_id="E2002", suffixes=("AAA",)):
    """A confirmed, undrafted folder in a tab, one document per suffix; returns (folder_id, document_ids)."""
    pages = [(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(len(suffixes))]
    batch = split.run_ingest_batch(pages, settings, folder_display_id=display_id, tab_id=tab_id)
    groups = [{"display_id": f"{display_id}{s}", "sequence_indices": [i]} for i, s in enumerate(suffixes)]
    made = split.confirm_batch_split(batch, groups, settings)
    with db.connect(settings.db_path) as conn:
        return db.get_new_document(conn, made[0])["folder_id"], made


def _draft(settings):
    new_document.run_transcribe_new_documents(settings, NewDocumentOptions(max_total_cost=10))


def _upload(client, tab_id, display_id="E2002"):
    return client.post("/api/new/batches", data={"folder_display_id": display_id, "tab_id": str(tab_id)},
                       files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])


def _accept_all(settings, folder_id):
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = 'accepted' WHERE folder_id = ?", (folder_id,))


def _export(settings, root, folder_id):
    jobs = folder_exports.Jobs(settings, root)
    job_id = jobs.start("new", folder_id, "Paul")
    deadline = time.monotonic() + 10
    while jobs.status(job_id)["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.02)
    return jobs.status(job_id)


# ---------- AC-1: slugs ----------


def test_ac1_a_slug_is_made_from_the_label():
    assert tabs.slugify("Ledgers & Accounts", set()) == "ledgers-accounts"


def test_ac1_a_reserved_or_taken_slug_gets_a_suffix():
    assert (tabs.slugify("Team", set()), tabs.slugify("Ledgers", {"ledgers"})) == ("team-2", "ledgers-2")


def test_ac1_a_label_without_a_letter_or_digit_is_refused():
    with pytest.raises(tabs.TabError):
        tabs.slugify("***", set())


def test_ac1_renaming_a_tab_keeps_its_slug(site):
    app, _ = site
    client = signed_in(app)
    tab = _tab(client, "Ledgers")
    client.put(f"/api/tabs/{tab['id']}", json={"label": "Account books"})
    found = next(t for t in client.get("/api/tabs").json()["tabs"] if t["id"] == tab["id"])
    assert (found["slug"], found["label"]) == ("ledgers", "Account books")


# ---------- AC-2: the list ----------


def test_ac2_tabs_are_listed_in_position_order(site):
    app, _ = site
    client = signed_in(app)
    a, b = _tab(client, "Ledgers"), _tab(client, "Photographs")
    client.post("/api/tabs/reorder", json={"order": [b["id"], a["id"], 1]})
    assert [t["slug"] for t in client.get("/api/tabs").json()["tabs"]] == ["photographs", "ledgers", "documents"]


def test_ac2_a_reviewer_receives_only_enabled_tabs_without_the_administrative_fields(site):
    app, _ = site
    paul = signed_in(app)
    paused = _tab(paul, "Ledgers")
    paul.put(f"/api/tabs/{paused['id']}", json={"enabled": False})
    seen = signed_in(app, "john").get("/api/tabs").json()["tabs"]
    assert [t["slug"] for t in seen] == ["documents"]
    assert set(seen[0]) == set(tabs.PUBLIC_FIELDS)


# ---------- AC-3: who may change what ----------


def test_ac3_an_admin_creates_renames_pauses_resumes_and_reorders(site):
    app, _ = site
    client = signed_in(app)
    tab = _tab(client, "Ledgers")
    steps = [client.put(f"/api/tabs/{tab['id']}", json={"label": "Books"}).status_code,
             client.put(f"/api/tabs/{tab['id']}", json={"enabled": False}).status_code,
             client.put(f"/api/tabs/{tab['id']}", json={"enabled": True}).status_code,
             client.post("/api/tabs/reorder", json={"order": [tab["id"]]}).status_code]
    first = client.get("/api/tabs").json()["tabs"][0]
    assert (steps, first["label"], first["enabled"]) == ([200] * 4, "Books", True)


@pytest.mark.parametrize("method,path", [("POST", "/api/tabs"), ("PUT", "/api/tabs/1"),
                                         ("POST", "/api/tabs/reorder"), ("DELETE", "/api/tabs/1"),
                                         ("POST", "/api/tabs/1/prompt-profile")])
def test_ac3_a_reviewer_is_refused_every_write(site, method, path):
    app, _ = site
    assert signed_in(app, "john").request(method, path, json={"label": "x", "order": [1]}).status_code == 403


# ---------- AC-4: the id scheme ----------


def test_ac4_the_id_scheme_changes_until_the_first_folder_exists(site):
    app, settings = site
    client = signed_in(app)
    tab = _tab(client, "Ledgers", id_scheme="numbers")
    before = client.put(f"/api/tabs/{tab['id']}", json={"id_scheme": "filename"}).status_code
    _folder(settings, tab["id"])
    after = client.put(f"/api/tabs/{tab['id']}", json={"id_scheme": "letters"})
    assert (before, after.status_code, "fixed" in after.json()["detail"]) == (200, 400, True)


def test_ac4_a_new_tabs_first_folder_follows_its_scheme(site):
    app, settings = site
    tab = _tab(signed_in(app), "Ledgers", id_scheme="numbers")
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="E2002",
                                   tab_id=tab["id"])
    assert split.suggest_document_ids(batch, settings)[0] == "E2002-001"


# ---------- AC-5: deleting ----------


def test_ac5_a_tab_holding_a_folder_cannot_be_deleted(site):
    app, settings = site
    client = signed_in(app)
    tab = _tab(client, "Ledgers")
    _folder(settings, tab["id"])
    refused = client.delete(f"/api/tabs/{tab['id']}")
    assert (refused.status_code, "folder" in refused.json()["detail"]) == (409, True)


def test_ac5_a_tab_holding_only_an_upload_cannot_be_deleted(site):
    app, settings = site
    client = signed_in(app)
    tab = _tab(client, "Ledgers")
    split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, tab_id=tab["id"])
    refused = client.delete(f"/api/tabs/{tab['id']}")
    assert (refused.status_code, "upload" in refused.json()["detail"]) == (409, True)


def test_ac5_an_empty_tab_can_be_deleted(site):
    app, _ = site
    client = signed_in(app)
    tab = _tab(client, "Ledgers")
    assert client.delete(f"/api/tabs/{tab['id']}").status_code == 200
    assert [t["slug"] for t in client.get("/api/tabs").json()["tabs"]] == ["documents"]


def test_ac5_the_last_tab_and_the_team_tabs_address_are_kept(site):
    app, _ = site
    client = signed_in(app)
    last = client.delete("/api/tabs/1").status_code
    assert (last, _tab(client, "Team")["slug"]) == (409, "team-2")


# ---------- AC-6: every listing narrows to a tab ----------


def test_ac6_folders_are_listed_per_tab(site):
    app, settings = site
    client = signed_in(app)
    ledgers = _tab(client, "Ledgers")
    _folder(settings, 1)
    other, _ = _folder(settings, ledgers["id"])
    listed = client.get(f"/api/new/folders?tab_id={ledgers['id']}").json()
    assert [(f["folder_id"], f["tab_slug"]) for f in listed] == [(other, "ledgers")]


def test_ac6_documents_batches_and_the_pending_count_are_per_tab(site, model):
    app, settings = site
    client = signed_in(app)
    ledgers = _tab(client, "Ledgers")
    _folder(settings, 1)
    _draft(settings)
    _folder(settings, ledgers["id"])
    split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, tab_id=ledgers["id"])
    counts = {
        "documents": [len(client.get(f"/api/new/documents?tab_id={t}").json()) for t in (1, ledgers["id"])],
        "batches": [len(client.get(f"/api/new/batches?tab_id={t}").json()) for t in (1, ledgers["id"])],
        "pending": [client.get(f"/api/new/pending-count?tab_id={t}").json()["pending"] for t in (1, ledgers["id"])],
    }
    assert counts == {"documents": [1, 0], "batches": [0, 1], "pending": [0, 1]}


def test_ac6_the_team_list_names_each_folders_tab(site):
    app, settings = site
    client = signed_in(app)
    ledgers = _tab(client, "Ledgers")
    _folder(settings, 1)
    _folder(settings, ledgers["id"])
    folders = client.get("/api/team/folders").json()["folders"]
    assert sorted(f["tab_slug"] for f in folders) == ["documents", "ledgers"]


# ---------- AC-7: uploads name their tab ----------


def test_ac7_an_upload_lands_in_the_tab_it_names(site):
    app, settings = site
    client = signed_in(app)
    ledgers = _tab(client, "Ledgers")
    batch_id = _upload(client, ledgers["id"]).json()["batch_id"]
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        folder = db.get_folder(conn, batch["folder_id"])
    assert (batch["tab_id"], folder["tab_id"]) == (ledgers["id"], ledgers["id"])


def test_ac7_two_tabs_each_hold_an_e2002_and_each_queue_shows_its_own(site, model):
    app, settings = site
    client = signed_in(app)
    ledgers = _tab(client, "Ledgers")
    first, _ = _folder(settings, 1)
    second, _ = _folder(settings, ledgers["id"])
    _draft(settings)
    queues = [client.get(f"/api/new/documents?tab_id={t}").json() for t in (1, ledgers["id"])]
    assert [(len(q), q[0]["folder_id"], q[0]["folder_display_id"]) for q in queues] == \
        [(1, first, "E2002"), (1, second, "E2002")]


def test_ac7_a_draft_uses_its_tabs_prompt_profile(site, model):
    app, settings = site
    client = signed_in(app)
    with db.connect(settings.db_path) as conn:
        profile_id = profiles.create_profile(conn, "Ledgers", {"collection": "the ledgers of a firm"}, None)
        wanted = profiles.profile(conn, profile_id)["current_version_id"]
        default = profiles.default_version_id(conn)
    ledgers = _tab(client, "Ledgers", prompt_profile_id=profile_id)
    _, [plain] = _folder(settings, 1)
    _, [ledger] = _folder(settings, ledgers["id"])
    _draft(settings)
    with db.connect(settings.db_path) as conn:
        pinned = [db.get_new_document(conn, d)["prompt_version_id"] for d in (plain, ledger)]
    assert pinned == [default, wanted]


# ---------- AC-8: a paused tab ----------


def test_ac8_a_paused_tab_leaves_a_reviewers_strip_and_returns_when_resumed(site):
    app, _ = site
    paul, john = signed_in(app), signed_in(app, "john")
    tab = _tab(paul, "Ledgers")
    paul.put(f"/api/tabs/{tab['id']}", json={"enabled": False})
    while_paused = [t["slug"] for t in john.get("/api/tabs").json()["tabs"]]
    paul.put(f"/api/tabs/{tab['id']}", json={"enabled": True})
    after = [t["slug"] for t in john.get("/api/tabs").json()["tabs"]]
    assert (while_paused, after) == (["documents"], ["documents", "ledgers"])


def test_ac8_a_folder_in_a_paused_tab_still_opens_but_takes_no_save(site, model):
    app, settings = site
    paul = signed_in(app)
    tab = _tab(paul, "Ledgers")
    _, [document_id] = _folder(settings, tab["id"])
    _draft(settings)
    paul.put(f"/api/tabs/{tab['id']}", json={"enabled": False})
    opened = paul.get(f"/api/new/documents/{document_id}").status_code
    saved = paul.post(f"/api/new/documents/{document_id}/review", json=REVIEW)
    assert (opened, saved.status_code, "Ledgers" in saved.json()["detail"]) == (200, 409, True)


def test_ac8_an_upload_and_a_draft_in_a_paused_tab_are_refused(site, model):
    app, settings = site
    paul = signed_in(app)
    tab = _tab(paul, "Ledgers")
    folder_id, _ = _folder(settings, tab["id"])
    paul.put(f"/api/tabs/{tab['id']}", json={"enabled": False})
    upload = _upload(paul, tab["id"], "E2003").status_code
    draft = paul.post(f"/api/new/folders/draft?folder_id={folder_id}").status_code
    assert (upload, draft) == (409, 409)


# ---------- AC-9: the strip and the hash ----------


def test_ac9_the_strip_is_drawn_from_the_tab_list():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'data-workflow="new"' not in page
    assert 'fetch("/api/tabs")' in app and "workflowTabs.insertBefore(btn, team)" in app


def test_ac9_team_stays_last_and_admin_only():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    admin_view = source[source.index("function applyAdminView()"):]
    assert 'tab.id = "tab-team"' in admin_view and "nav.appendChild(tab)" in admin_view[:1200]


def test_ac9_the_hash_names_the_tab_by_slug():
    result = run_history("""
      window.__tabs__ = ['documents', 'ledgers'];
      assert.deepEqual(R.routeOf('#ledgers/f1/d1'), { area: 'doc', tab: 'ledgers', folder: 'f1', doc: 'd1' });
      assert.equal(R.routeOf('#nope/f1'), null);
      assert.equal(R.hashOf({ area: 'queue', tab: 'ledgers', folder: 'f1', doc: null }), '#ledgers/f1');""")
    assert result.returncode == 0, result.stderr


# ---------- AC-10: Settings → Tabs and the wizard ----------


def test_ac10_settings_has_a_tabs_panel_and_the_wizard_a_tabs_step(site):
    app, _ = site
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    steps = [s["id"] for s in signed_in(app, "olive").get("/api/setup").json()["steps"]]
    assert 'data-settings-tab="tabs"' in page and "tabs_admin.js" in page
    assert steps.index("prompt") < steps.index("tabs") < steps.index("people")


def test_ac10_the_prompt_settings_no_longer_pick_tabs(site):
    app, _ = site
    builder = (STATIC / "prompt_builder.js").read_text(encoding="utf-8")
    listing = signed_in(app).get("/api/prompt-profiles").json()
    assert "Which tabs use it" not in builder and "data-tab" not in builder
    assert "tabs" not in listing


def test_ac10_a_tabs_profile_is_set_from_the_tabs_settings(site):
    app, settings = site
    client = signed_in(app)
    with db.connect(settings.db_path) as conn:
        profile_id = profiles.create_profile(conn, "Ledgers", {}, None)
    changed = client.put("/api/tabs/1", json={"prompt_profile_id": profile_id}).json()["tab"]
    assert (changed["prompt_profile_id"], changed["prompt_profile_name"]) == (profile_id, "Ledgers")


# ---------- AC-11: exports grouped by tab ----------


def test_ac11_a_folders_export_is_published_under_its_tabs_slug(site, model, tmp_path):
    app, settings = site
    ledgers = _tab(signed_in(app), "Ledgers")
    folder_id, _ = _folder(settings, ledgers["id"])
    _draft(settings)
    _accept_all(settings, folder_id)
    state = _export(settings, tmp_path / "exports", folder_id)
    assert state["state"] == "completed", state
    assert (tmp_path / "exports" / "ledgers" / folder_id).is_dir()


def test_ac11_same_named_folders_in_two_tabs_export_apart(site, model, tmp_path):
    app, settings = site
    ledgers = _tab(signed_in(app), "Ledgers")
    first, _ = _folder(settings, 1)
    second, _ = _folder(settings, ledgers["id"])
    _draft(settings)
    for folder_id in (first, second):
        _accept_all(settings, folder_id)
        assert _export(settings, tmp_path / "exports", folder_id)["state"] == "completed"
    assert sorted(p.name for p in (tmp_path / "exports").iterdir()) == ["documents", "ledgers"]
