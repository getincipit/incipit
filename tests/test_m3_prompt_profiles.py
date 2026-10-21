"""Covers M3 (docs/specs/M3-prompt-profiles.md): versioned prompt profiles, the field registry, index
styles, the review payload, the migration of old documents, the prompt test and the builder UI."""

import csv
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from incipit import db, export, fields, new_document, openrouter, profiles, prompting, split, tabs
from incipit.new_document import NewDocumentOptions, document_title
from conftest import bill, make_app, next_document_ids, signed_in, tiny_image_bytes

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
DEFAULT = prompting.compile({})


def _compile(**answers):
    return prompting.compile(answers)


def _styles(**styles):
    return prompting.compile({"styles": styles}).document_prompt


@pytest.fixture
def site(tmp_path):
    """Olive the owner, Paul an admin, John a reviewer, and a model key for the prompt test."""
    app, settings = make_app(tmp_path, owner="olive", admins=("paul",), reviewers=("john",))
    settings.openrouter_api_key = "sk-test"
    return app, settings


def _answer(extract_result, **values):
    parsed = {"text": "Dear Sir,\n\nThe shipment left on Monday.", "uncertain_words": [],
              "document_type": "Letter", "date": "1901-03-04", "author": "Smith, Jane",
              "recipient": "Brown, Thomas", "place": "Boston, MA", "names": [], "subjects": ["Shipping"],
              "places": ["Boston, MA"], "marginal_notes": [], "challenges": ["typed"], **values}
    return extract_result(parsed, {"cost": 0.001})


@pytest.fixture
def model(monkeypatch):
    """A model that drafts every document the same way, remembering the schema it was asked for."""
    asked = []

    def fake(messages, settings, schema, extract_result, extra=None):
        asked.append(schema)
        bill(settings.db_path, 0.001)
        return _answer(extract_result)
    monkeypatch.setattr(openrouter, "call_openrouter", fake)
    return asked


def _drafted(settings, display_id="F001AAA"):
    """One folder with one document, drafted by the fake model; returns its internal id."""
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F001")
    with next_document_ids(display_id):
        [document_id] = split.confirm_batch_split(batch, [{"display_id": display_id, "sequence_indices": [0]}],
                                                  settings)
    new_document.run_transcribe_new_documents(settings, NewDocumentOptions(max_total_cost=10))
    return document_id


def _new_version(settings, answers):
    with db.connect(settings.db_path) as conn:
        profile_id = conn.execute("SELECT id FROM prompt_profiles ORDER BY id LIMIT 1").fetchone()[0]
        return profiles.add_version(conn, profile_id, answers, None)


def _save(client, document_id, **changes):
    document = client.get(f"/api/new/documents/{document_id}").json()
    return client.post(f"/api/new/documents/{document_id}/review", json={
        "reviewed_text": document["transcription"]["text"], "title": "", "review_status": "accepted",
        "editors_notes": "", "fields": changes})


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(out, format="PNG")
    return out.getvalue()


# ---------- AC-1: the default prompt ----------


def test_ac1_the_default_prompt_has_one_slot_for_each_page_count():
    assert (DEFAULT.document_prompt.count("{page_count}"), DEFAULT.document_prompt.count("{page_break_count}")) == (1, 1)


def test_ac1_the_default_prompt_carries_the_fidelity_tagging_and_marginal_rules():
    prompt = " ".join(DEFAULT.document_prompt.split())
    assert all(rule in prompt for rule in ("Never write a word the page does not", "MANDATORY, not optional",
                                           "Marginal notes belong in `marginal_notes`"))


def test_ac1_the_default_prompts_field_list_is_the_registry():
    import re
    listed = set(re.findall(r"^- `([a-z_]+)`:", DEFAULT.document_prompt, flags=re.MULTILINE))
    assert listed == {"text", "uncertain_words"} | {f["key"] for f in fields.enabled(DEFAULT.registry)}


def test_ac1_the_default_prompts_name_no_person_place_or_archive():
    from test_fork_residue import PROPRIETARY
    assert not any(PROPRIETARY.search(p) for p in (DEFAULT.document_prompt, DEFAULT.split_prompt, DEFAULT.merge_prompt))


# ---------- AC-2: each index style changes the prompt ----------


@pytest.mark.parametrize("styles,expected", [
    ({"names": "first_last"}, "`First Middle Last`"),
    ({"names": "as_written"}, "exactly as the document writes it"),
    ({"us_places": "full_state"}, "state written in full"),
    ({"us_places": "as_written"}, "as the document writes"),
    ({"world_places": "as_written"}, "as the document writes"),
    ({"dates": "as_written"}, "as the document writes"),
    ({"separator": " | "}, '" | "'),
    ({"uncertain": "bracket_illegible"}, "`[illegible]`"),
    ({"uncertain": "question"}, "`word[?]`"),
    ({"marginal_notes": "inline"}, "Marginal notes stay in `text`"),
    ({"marginal_notes": "ignore"}, "Leave marginal annotations out"),
    ({"crossouts": "marked"}, "crossed"),
])
def test_ac2_each_index_style_is_written_into_the_prompt(styles, expected):
    assert expected.lower() in " ".join(_styles(**styles).split()).lower()


@pytest.mark.parametrize("switch,phrase", [("structural_tags", "Structural tagging is MANDATORY"),
                                           ("overwritten_hand", "Handwriting written over a typed page")])
def test_ac2_a_switched_off_style_leaves_its_paragraph_out(switch, phrase):
    assert phrase not in _styles(**{switch: False})


def test_ac2_the_known_entities_section_appears_only_when_an_entity_is_given():
    with_one = _compile(people=[{"name": "Okafor, Grace", "note": "secretary"}]).document_prompt
    assert ("Okafor, Grace" in with_one, "Okafor" in DEFAULT.document_prompt) == (True, False)


# ---------- AC-3: typed text cannot add a placeholder ----------


@pytest.mark.parametrize("answer", ["collection", "material", "region", "special_instructions"])
def test_ac3_braces_in_an_answer_survive_format_unchanged(answer):
    rendered = _compile(**{answer: "Papers of the {x} society {0}"}).document_prompt.format(page_count=2, page_break_count=1)
    assert "Papers of the {x} society {0}" in rendered


def test_ac3_braces_in_an_entity_survive_the_merge_prompt():
    merge = _compile(people=[{"name": "Doe {John}"}]).merge_prompt
    assert merge.format(document_text="T").count("{document_text}") == 0


# ---------- AC-4: the content hash ----------


def test_ac4_the_hash_is_the_same_for_the_same_answers():
    assert _compile(collection="A").content_hash == _compile(collection="A").content_hash


@pytest.mark.parametrize("answers", [{"collection": "B"}, {"styles": {"dates": "as_written"}},
                                     {"fields": {"place": {"enabled": False}}}, {"special_instructions": "x"}])
def test_ac4_the_hash_changes_when_any_answer_changes(answers):
    assert prompting.compile(answers).content_hash != DEFAULT.content_hash


# ---------- AC-5: one registry drives the schema, extractor, storage, form and export ----------

CUSTOM = {"fields": {"place": {"enabled": False}},
          "custom_fields": [{"key": "language", "label": "Language", "type": "string", "export_column": "Language"}]}


def test_ac5_the_schema_follows_the_registry():
    properties = prompting.compile(CUSTOM).document_schema["json_schema"]["schema"]["properties"]
    assert ("place" in properties, "language" in properties) == (False, True)


def test_ac5_the_extractor_follows_the_registry():
    compiled = prompting.compile(CUSTOM)
    got = openrouter.document_extractor(compiled.registry)({"text": "t", "place": "X", "language": "French"}, {})
    assert ("place" in got["fields"], got["fields"]["language"]) == (False, "French")


def test_ac5_the_review_form_follows_the_registry():
    keys = {f["key"] for f in fields.describe(prompting.compile(CUSTOM).registry)}
    assert ("place" in keys, "language" in keys) == (False, True)


def test_ac5_the_export_columns_follow_the_registry():
    columns = fields.export_columns(prompting.compile(CUSTOM).registry)
    assert ("Language" in columns, columns[-3:]) == (True, list(fields.TRAILING_COLUMNS))


def test_ac5_a_drafted_custom_field_is_stored_and_exported(site, monkeypatch):
    _, settings = site
    with db.connect(settings.db_path) as conn:
        profile_id = profiles.create_profile(conn, "Custom", CUSTOM, None)
        tabs.update(conn, 1, {"prompt_profile_id": profile_id})
    monkeypatch.setattr(openrouter, "call_openrouter",
                        lambda messages, s, schema, extract_result, extra=None: _answer(extract_result, language="French"))
    document_id = _drafted(settings)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = 'accepted' WHERE document_id = ?", (document_id,))
    _, body, _ = export.export_new_csv(settings)
    assert next(csv.DictReader(io.StringIO(body.decode("utf-8"))))["Language"] == "French"


# ---------- AC-6: versions are immutable ----------


def test_ac6_the_database_refuses_to_change_a_version(site):
    _, settings = site
    with db.connect(settings.db_path) as conn, pytest.raises(db.DatabaseError, match="cannot be changed"):
        conn.execute("UPDATE prompt_profile_versions SET compiled_document_prompt = 'x'")


def test_ac6_the_database_refuses_to_delete_a_version(site):
    _, settings = site
    with db.connect(settings.db_path) as conn, pytest.raises(db.DatabaseError, match="cannot be deleted"):
        conn.execute("DELETE FROM prompt_profile_versions")


def test_ac6_there_is_no_route_that_edits_a_version(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        version_id = profiles.default_version_id(conn)
    assert signed_in(app, "olive").put(f"/api/prompt-profiles/versions/{version_id}", json={}).status_code == 405


def test_ac6_editing_the_answers_makes_a_new_version(site):
    app, settings = site
    client = signed_in(app, "olive")
    profile_id = client.get("/api/prompt-profiles").json()["profiles"][0]["id"]
    client.post(f"/api/prompt-profiles/{profile_id}/versions", json={"answers": {"collection": "the papers of a society"}})
    assert [v["version_no"] for v in client.get(f"/api/prompt-profiles/{profile_id}/versions").json()["versions"]] == [2, 1]


# ---------- AC-7: documents pin their version ----------


def test_ac7_a_new_draft_records_the_tabs_current_version(site, model):
    _, settings = site
    version_id = _new_version(settings, {"collection": "the papers of a society"})
    document_id = _drafted(settings)
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document(conn, document_id)["prompt_version_id"] == version_id


def test_ac7_an_older_document_keeps_its_versions_field_label_after_a_rename(site, model):
    app, settings = site
    document_id = _drafted(settings)
    _new_version(settings, {"fields": {"author": {"label": "Writer"}}})
    labels = {f["key"]: f["label"] for f in signed_in(app).get(f"/api/new/documents/{document_id}").json()["field_registry"]}
    assert labels["author"] == "Author"


def test_ac7_an_older_document_still_reviews_a_field_a_later_version_removed(site, model):
    app, settings = site
    document_id = _drafted(settings)
    _new_version(settings, {"fields": {"place": {"enabled": False}}})
    assert _save(signed_in(app), document_id, place="Salem, MA").status_code == 200


def test_ac7_an_older_document_still_exports_a_field_a_later_version_removed(site, model):
    app, settings = site
    document_id = _drafted(settings)
    _new_version(settings, {"fields": {"author": {"enabled": False}}})
    _save(signed_in(app), document_id)
    _, body, _ = export.export_new_csv(settings)
    assert next(csv.DictReader(io.StringIO(body.decode("utf-8"))))["Author(s)"] == "Smith, Jane"


def test_ac7_repinning_a_folder_moves_its_documents_without_redrafting(site, model):
    app, settings = site
    document_id = _drafted(settings)
    version_id = _new_version(settings, {"collection": "the papers of a society"})
    with db.connect(settings.db_path) as conn:
        folder_id = db.get_new_document(conn, document_id)["folder_id"]
    signed_in(app, "olive").post(f"/api/new/folders/{folder_id}/prompt-version", json={"version_id": version_id})
    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, document_id)
    assert (row["prompt_version_id"], fields.drafted(row)["author"], len(model)) == (version_id, "Smith, Jane", 1)


# ---------- AC-8: the review payload is checked against the registry ----------


def test_ac8_an_unknown_field_is_refused(site, model):
    app, settings = site
    assert _save(signed_in(app), _drafted(settings), shoe_size="9").status_code == 400


def test_ac8_a_disabled_field_is_ignored(site, monkeypatch):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        tabs.update(conn, 1, {"prompt_profile_id": profiles.create_profile(conn, "No place", {"fields": {"place": {"enabled": False}}}, None)})
    monkeypatch.setattr(openrouter, "call_openrouter", lambda m, s, schema, extract_result, extra=None: _answer(extract_result))
    document_id = _drafted(settings)
    _save(signed_in(app), document_id, place="Salem, MA")
    with db.connect(settings.db_path) as conn:
        assert "place" not in fields.reviewed(db.get_new_document(conn, document_id))


def test_ac8_a_list_field_takes_a_list(site, model):
    app, settings = site
    assert _save(signed_in(app), _drafted(settings), names="Smith, Jane").status_code == 400


def test_ac8_a_choice_field_takes_only_its_options(site, model):
    app, settings = site
    assert _save(signed_in(app), _drafted(settings), challenges=["water damage"]).status_code == 400


# ---------- AC-9: documents from before M3 keep every value ----------


# ---------- AC-10: the title template ----------

VALUES = {"document_type": "Letter", "author": "Smith, Jane", "recipient": "Brown, Thomas", "date": "1901-03-04"}


def test_ac10_last_first_names_are_turned_round_for_the_title():
    assert document_title(DEFAULT, "A1", VALUES) == "[A1], Letter from Jane Smith to Thomas Brown, March 4th, 1901"


def test_ac10_names_written_first_last_are_left_alone():
    compiled = _compile(styles={"names": "first_last"})
    assert "from Smith, Jane to" in document_title(compiled, "A1", VALUES)


def test_ac10_dates_are_written_long_only_under_iso():
    compiled = _compile(styles={"dates": "as_written"})
    assert document_title(compiled, "A1", {**VALUES, "date": "1901-03-04"}).endswith(", 1901-03-04")


def test_ac10_a_template_names_its_own_fields_and_drops_empty_clauses():
    compiled = _compile(title_template="{display_id}: {document_type} ({place})")
    assert document_title(compiled, "A1", {"document_type": "Deed"}) == "A1: Deed"


# ---------- AC-11: a refused answer keeps the current version ----------


def test_ac11_an_invalid_answer_refuses_the_new_version(site):
    app, _ = site
    client = signed_in(app, "olive")
    profile_id = client.get("/api/prompt-profiles").json()["profiles"][0]["id"]
    refused = client.post(f"/api/prompt-profiles/{profile_id}/versions", json={"answers": {"period_from": "soon"}})
    versions = client.get(f"/api/prompt-profiles/{profile_id}/versions").json()["versions"]
    assert (refused.status_code, len(versions)) == (400, 1)


def test_ac11_the_preview_says_what_is_wrong(site):
    app, _ = site
    refused = signed_in(app, "olive").post("/api/prompt-profiles/preview", json={"answers": {"title_template": "{date}"}})
    assert "display_id" in refused.json()["detail"]


def test_ac11_a_custom_field_that_clashes_with_a_built_in_is_refused():
    with pytest.raises(prompting.CompileError, match="already used"):
        prompting.compile({"custom_fields": [{"key": "author", "label": "Author", "type": "string"}]})


# ---------- AC-12: the prompt test ----------


def test_ac12_the_prompt_test_returns_the_drafted_fields(site, model):
    app, _ = site
    client = signed_in(app, "olive")
    profile_id = client.get("/api/prompt-profiles").json()["profiles"][0]["id"]
    got = client.post(f"/api/prompt-profiles/{profile_id}/test", files=[("files", ("a.png", _png(), "image/png"))])
    assert got.json()["fields"]["author"] == "Smith, Jane"


def test_ac12_the_prompt_tests_cost_counts_toward_the_cap(site, model):
    app, settings = site
    client = signed_in(app, "olive")
    profile_id = client.get("/api/prompt-profiles").json()["profiles"][0]["id"]
    client.post(f"/api/prompt-profiles/{profile_id}/test", files=[("files", ("a.png", _png(), "image/png"))])
    with db.connect(settings.db_path) as conn:
        assert db.get_total_cost(conn) == pytest.approx(0.001)


def test_ac12_the_prompt_test_stops_at_the_cap(site, model):
    app, settings = site
    settings.max_total_cost = 0.0
    client = signed_in(app, "olive")
    profile_id = client.get("/api/prompt-profiles").json()["profiles"][0]["id"]
    got = client.post(f"/api/prompt-profiles/{profile_id}/test", files=[("files", ("a.png", _png(), "image/png"))])
    assert (got.status_code, len(model)) == (409, 0)


def test_ac12_a_reviewer_cannot_run_the_prompt_test(site, model):
    app, _ = site
    got = signed_in(app, "john").post("/api/prompt-profiles/1/test", files=[("files", ("a.png", _png(), "image/png"))])
    assert (got.status_code, len(model)) == (403, 0)


# ---------- AC-13: Settings → Prompt and the wizard step ----------


def test_ac13_settings_has_a_prompt_tab_that_mounts_the_builder():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-settings-tab="prompt"' in page and "prompt_builder.js" in page


def test_ac13_the_wizard_has_a_prompt_step(site):
    app, _ = site
    steps = [s["id"] for s in signed_in(app, "olive").get("/api/setup").json()["steps"]]
    assert steps.index("site") < steps.index("prompt") < steps.index("people")


def test_ac13_a_reviewers_page_loses_the_prompt_tab():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert '["settings", "deletions", "site", "appearance", "prompt", "tabs", "models", "usage", "backups", "updates", "access"].forEach' in source


@pytest.mark.parametrize("path", ["/api/prompt-profiles", "/api/prompt-profiles/questionnaire"])
def test_ac13_a_reviewer_cannot_reach_the_profile_routes(site, path):
    app, _ = site
    assert signed_in(app, "john").get(path).status_code == 403


def test_ac13_a_reviewer_cannot_choose_a_tabs_profile(site):
    app, _ = site
    assert signed_in(app, "john").post("/api/tabs/1/prompt-profile", json={"profile_id": 1}).status_code == 403


def test_ac13_the_builders_diff_marks_what_changed():
    import subprocess
    script = (STATIC / "page-kit.js").read_text(encoding="utf-8") + (STATIC / "prompt_builder.js").read_text(encoding="utf-8")
    harness = ("const window = globalThis; " + script +
               "\nconsole.log(JSON.stringify(window.PromptBuilder.lineDiff('a\\nb\\nc', 'a\\nB\\nc')));")
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [["same", "a"], ["gone", "b"], ["new", "B"], ["same", "c"]]
