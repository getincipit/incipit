"""Covers M5 (docs/specs/M5-model-access-ledger-and-jobs.md): the encrypted key and the Models page,
the free connection test, the call ledger and the caps it feeds, the Usage page, and drafting and
split proposals as jobs that survive a closed browser and a restart."""

import json
import sqlite3
import subprocess

import pytest
import requests

from incipit import (accounts, backends, cli, datafolder, db, jobs, keyvault, ledger, new_document, openrouter,
                     review_server, site_settings, split, startup)
from incipit.new_document import NewDocumentOptions
from conftest import bill, next_document_ids, signed_in, tiny_image_bytes

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _site(tmp_path, monkeypatch):
    """An app run from a fresh data folder, with Olive the owner, Paul an admin and John a reviewer."""
    folder = tmp_path / "Incipit"
    datafolder.initialise(folder)
    config = datafolder.load(folder)
    settings = datafolder.settings_for(folder, config)
    paths = datafolder.resolved_paths(folder, config)
    monkeypatch.setattr(startup, "run", lambda args: subprocess.CompletedProcess(args, 1, "", ""))
    app = review_server.create_app(settings, str(paths["exports"]), str(paths["logs"]))
    with db.connect(settings.db_path) as conn:
        accounts.create_owner(conn, "olive", "olive-password", "Olive")
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, settings, folder


@pytest.fixture
def site(tmp_path, monkeypatch):
    return _site(tmp_path, monkeypatch)


class FakeReply:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _answer(text="Dear Sir, the patent is filed.", cost=0.002):
    content = json.dumps({"text": text, "uncertain_words": [], "document_type": "Letter"})
    return FakeReply(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "cost": cost}})


def _service(monkeypatch, replies):
    """The model service answering each POST with the next reply (or raising it, for a timeout)."""
    sent = []

    class Session:
        def post(self, url, **kwargs):
            sent.append(kwargs.get("json"))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
    monkeypatch.setattr(openrouter, "_session_with_retries", lambda retries: Session())
    return sent


def _call(settings, purpose="draft"):
    from incipit import prompting
    compiled = prompting.compile({})
    with ledger.context(purpose=purpose):
        return openrouter.call_openrouter([{"role": "user", "content": "x"}], settings,
                                          schema=compiled.document_schema,
                                          extract_result=openrouter.document_extractor(compiled.registry))


def _calls(settings):
    ledger.flush()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM model_calls WHERE purpose != 'carried_over' ORDER BY id")]
    finally:
        conn.close()


def _keyed(settings):
    settings.openrouter_api_key = "sk-or-test-key-123456"
    return settings


# ---------- AC-1: the key ----------


def test_ac1_the_owner_saves_a_key_and_only_its_hint_comes_back(site):
    app, _, _ = site
    got = signed_in(app, "olive").put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    assert got.json()["key_hint"] == "…3456" and "abcdef" not in got.text


def test_ac1_the_key_is_stored_encrypted(site):
    app, settings, _ = site
    signed_in(app, "olive").put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    with db.connect(settings.db_path) as conn:
        stored = backends.get(conn)["api_key_enc"]
    assert stored and "abcdef" not in stored


def test_ac1_an_admin_cannot_change_the_key(site):
    app, _, _ = site
    assert signed_in(app, "paul").put("/api/models", json={"api_key": "sk-or-v1-x"}).status_code == 403


def test_ac1_a_key_in_the_file_moves_into_the_database_and_leaves_the_file(tmp_path):
    folder = tmp_path / "Data"
    datafolder.initialise(folder)
    path = datafolder.config_path(folder)
    path.write_text(path.read_text(encoding="utf-8").replace('openrouter_api_key = ""',
                                                             'openrouter_api_key = "sk-or-v1-fromfile9999"'),
                    encoding="utf-8")
    settings = datafolder.settings_for(folder, datafolder.load(folder))
    from incipit import bootstrap
    bootstrap.initialise(settings.db_path)
    cli.adopt_model_key(folder, settings)
    with db.connect(settings.db_path) as conn:
        hint = backends.public(conn)["key_hint"]
    assert hint == "…9999" and "fromfile" not in path.read_text(encoding="utf-8")


# ---------- AC-2: the encryption key stays on this computer ----------


def test_ac2_a_database_taken_elsewhere_cannot_read_the_key(site, monkeypatch, tmp_path):
    app, settings, _ = site
    signed_in(app, "olive").put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    monkeypatch.setenv("INCIPIT_SETTINGS_DIR", str(tmp_path / "another-computer"))
    with db.connect(settings.db_path) as conn:
        shown = backends.public(conn)
    assert shown["has_key"] is False and "enter it again" in shown["key_problem"]


def test_ac2_the_encryption_key_is_not_in_the_data_folder(site):
    app, _, folder = site
    signed_in(app, "olive").put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    assert not list(folder.rglob(keyvault.KEY_NAME))


# ---------- AC-3: testing the connection ----------


def _probe(monkeypatch, reply):
    asked = []

    def fake_get(url, **kwargs):
        asked.append(url)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(backends.requests, "get", fake_get)
    return asked


def test_ac3_a_working_key_is_reported_with_its_balance(site, monkeypatch):
    app, _, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    _probe(monkeypatch, FakeReply(200, {"data": {"usage": 1.5, "limit_remaining": 8.25}}))
    got = client.post("/api/models/test").json()
    assert got["ok"] and "$8.25" in got["detail"]


def test_ac3_a_refused_key_is_reported(site, monkeypatch):
    app, _, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    _probe(monkeypatch, FakeReply(401, {"error": {"message": "No auth credentials found"}}))
    got = client.post("/api/models/test").json()
    assert not got["ok"] and "refused" in got["detail"]


def test_ac3_an_unreachable_server_is_reported(site, monkeypatch):
    app, _, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"kind": "openai_compatible", "base_url": "http://192.168.1.50:8000/v1"})
    _probe(monkeypatch, requests.exceptions.ConnectionError("refused"))
    got = client.post("/api/models/test").json()
    assert not got["ok"] and "192.168.1.50" in got["detail"]


def test_ac3_the_test_spends_nothing(site, monkeypatch):
    app, settings, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    asked = _probe(monkeypatch, FakeReply(200, {"data": {"usage": 0}}))
    client.post("/api/models/test")
    assert asked == ["https://openrouter.ai/api/v1/key"] and _calls(settings) == []


# ---------- AC-4: choosing the models ----------


def test_ac4_a_new_model_is_used_by_the_next_call_without_a_restart(site, monkeypatch):
    app, settings, _ = site
    signed_in(app, "olive").put("/api/models", json={"api_key": "sk-or-v1-abcdef123456",
                                                     "model": "google/gemma-4-vision:free"})
    sent = _service(monkeypatch, [_answer()])
    _call(settings)
    assert sent[0]["model"] == "google/gemma-4-vision:free"


def test_ac4_a_self_hosted_server_is_called_at_its_address(site):
    app, settings, _ = site
    signed_in(app, "olive").put("/api/models", json={"kind": "openai_compatible", "base_url": "http://10.0.0.7:8000/v1/",
                                                     "api_key": "local-key-000000"})
    assert settings.openrouter_base_url == "http://10.0.0.7:8000/v1"


# ---------- AC-5: the ledger ----------


def test_ac5_a_successful_call_is_recorded_with_its_cost_and_tokens(tmp_path, site, monkeypatch):
    _, settings, _ = site
    _service(monkeypatch, [_answer(cost=0.0031)])
    _call(_keyed(settings))
    [row] = _calls(settings)
    assert (row["outcome"], row["cost_usd"], row["input_tokens"], row["purpose"]) == ("ok", 0.0031, 1200, "draft")


def test_ac5_a_malformed_attempt_is_recorded_as_well_as_the_good_one(site, monkeypatch):
    _, settings, _ = site
    bad = FakeReply(200, {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
                          "usage": {"cost": 0.0012}})
    _service(monkeypatch, [bad, _answer(cost=0.002)])
    _call(_keyed(settings))
    assert [r["outcome"] for r in _calls(settings)] == ["malformed", "ok"]


def test_ac5_a_refused_call_is_recorded(site, monkeypatch):
    _, settings, _ = site
    _service(monkeypatch, [FakeReply(402, {"error": {"message": "Insufficient credits"}})])
    with pytest.raises(openrouter.AccountError):
        _call(_keyed(settings))
    [row] = _calls(settings)
    assert (row["outcome"], row["http_status"], row["cost_usd"]) == ("refused", 402, 0.0)


def test_ac5_a_timed_out_call_is_recorded_as_ambiguous_with_an_estimate(site, monkeypatch):
    _, settings, _ = site
    _service(monkeypatch, [requests.exceptions.ReadTimeout("slow")])
    with pytest.raises(openrouter.TransientModelError):
        _call(_keyed(settings))
    [row] = _calls(settings)
    assert (row["outcome"], row["cost_source"]) == ("ambiguous", "estimated") and row["cost_usd"] > 0


def test_ac5_ledger_rows_cannot_be_changed_or_deleted(site):
    _, settings, _ = site
    bill(settings.db_path, 0.01)
    conn = sqlite3.connect(settings.db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
            conn.execute("UPDATE model_calls SET cost_usd = 0")
        with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
            conn.execute("DELETE FROM model_calls")
    finally:
        conn.close()


# ---------- AC-6: spend is the ledger ----------


def _drafted(settings, display_id="F001AAA"):
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F001")
    with next_document_ids(display_id):
        [document_id] = split.confirm_batch_split(batch, [{"display_id": display_id, "sequence_indices": [0]}],
                                                  settings)
    return document_id


def test_ac6_deleting_a_drafted_document_never_lowers_spend(site, monkeypatch):
    app, settings, _ = site
    _service(monkeypatch, [_answer(cost=0.004)])
    document_id = _drafted(_keyed(settings))
    new_document.run_transcribe_new_documents(settings, NewDocumentOptions(max_total_cost=10))
    with db.connect(settings.db_path) as conn:
        before = db.get_total_cost(conn)
        conn.execute("DELETE FROM new_document_pages WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM new_documents WHERE document_id = ?", (document_id,))
    with db.connect(settings.db_path) as conn:
        assert before == pytest.approx(0.004) and db.get_total_cost(conn) == pytest.approx(before)


def test_ac6_a_malformed_attempts_cost_counts(site, monkeypatch):
    _, settings, _ = site
    bad = FakeReply(200, {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
                          "usage": {"cost": 0.0012}})
    _service(monkeypatch, [bad, _answer(cost=0.002)])
    _call(_keyed(settings))
    with db.connect(settings.db_path) as conn:
        assert db.get_total_cost(conn) == pytest.approx(0.0032)


# ---------- AC-7: the caps ----------


def test_ac7_the_monthly_cap_stops_drafting_and_says_so(site, monkeypatch):
    _, settings, _ = site
    _drafted(_keyed(settings))
    bill(settings.db_path, 0.5)
    sent = _service(monkeypatch, [_answer()])
    outcome = new_document.run_transcribe_new_documents(
        settings, NewDocumentOptions(max_total_cost=10, monthly_cap=0.5))
    assert sent == [] and "monthly cap of $0.50" in outcome["stopped_by_cap"]


def test_ac7_the_all_time_cap_stops_a_split_proposal(site, monkeypatch):
    _, settings, _ = site
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F002")
    bill(settings.db_path, 3.0)
    settings.max_total_cost = 2.0
    with pytest.raises(RuntimeError, match="all-time cap"):
        split.run_propose_split(batch, settings)


def test_ac7_an_admin_sets_the_monthly_cap(site):
    app, settings, _ = site
    got = signed_in(app, "paul").put("/api/usage/caps", json={"monthly_cap": "25"})
    assert got.json()["monthly_cap"] == 25.0 and settings.monthly_cap == 25.0


def test_ac7_an_empty_monthly_cap_means_none(site):
    app, _, _ = site
    client = signed_in(app, "paul")
    client.put("/api/usage/caps", json={"monthly_cap": "25"})
    assert client.put("/api/usage/caps", json={"monthly_cap": ""}).json()["monthly_cap"] is None


# ---------- AC-8: the Usage page ----------


def test_ac8_usage_shows_spend_by_month_purpose_and_model(site):
    app, settings, _ = site
    bill(settings.db_path, 0.03, "draft")
    bill(settings.db_path, 0.01, "split")
    got = signed_in(app, "paul").get("/api/usage").json()
    purposes = {r["name"]: r["cost"] for r in got["by_purpose"]}
    assert got["this_month"] == pytest.approx(0.04) and purposes == pytest.approx({"draft": 0.03, "split": 0.01})


def test_ac8_a_reviewer_is_refused_usage_and_models(site):
    app, _, _ = site
    john = signed_in(app, "john")
    assert (john.get("/api/usage").status_code, john.get("/api/models").status_code) == (403, 403)


def test_ac8_a_job_payload_carries_no_cost(site):
    app, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1"])
    shown = signed_in(app, "paul").get(f"/api/jobs/{job['id']}").json()
    assert not any("cost" in key for key in shown)


# ---------- AC-9: jobs ----------


def test_ac9_drafting_a_folder_is_recorded_as_a_job_that_finishes(site, monkeypatch):
    app, settings, _ = site
    document_id = _drafted(_keyed(settings))
    _service(monkeypatch, [_answer()])
    with db.connect(settings.db_path) as conn:
        folder_id = db.folder_of_document(conn, document_id)
    signed_in(app, "olive").post(f"/api/new/folders/draft?folder_id={folder_id}")
    [job] = jobs.listing(settings.db_path, folder_id)
    assert (job["kind"], job["state"], job["done"], job["total"]) == ("draft", "succeeded", 1, 1)


def test_ac9_a_jobs_calls_carry_its_id_in_the_ledger(site, monkeypatch):
    app, settings, _ = site
    document_id = _drafted(_keyed(settings))
    _service(monkeypatch, [_answer()])
    with db.connect(settings.db_path) as conn:
        folder_id = db.folder_of_document(conn, document_id)
    signed_in(app, "olive").post(f"/api/new/folders/draft?folder_id={folder_id}")
    [job] = jobs.listing(settings.db_path, folder_id)
    [row] = _calls(settings)
    assert (row["job_id"], row["document_id"], row["purpose"]) == (job["id"], document_id, "draft")


def test_ac9_a_second_request_for_the_same_folder_joins_the_first_job(site):
    _, settings, _ = site
    first = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1"])
    again = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1", "d2"])
    assert again["id"] == first["id"]


def test_ac9_a_split_proposal_is_recorded_as_a_job(site, monkeypatch):
    app, settings, _ = site
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F003")
    monkeypatch.setattr(review_server, "run_propose_split", lambda batch_id, s: {0: True})
    signed_in(app, "olive").post(f"/api/new/batches/{batch}/retry-proposal")
    [job] = [j for j in jobs.listing(settings.db_path) if j["batch_id"] == batch]
    assert (job["kind"], job["state"]) == ("split", "succeeded")


def test_ac9_progress_is_readable_while_a_job_runs(site):
    app, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1", "d2"])
    jobs._update(settings.db_path, job["id"], state="running", done=1, current_document="d2")
    shown = signed_in(app, "paul").get(f"/api/jobs/{job['id']}").json()
    assert (shown["state"], shown["done"], shown["total"], shown["current_document"]) == ("running", 1, 2, "d2")


# ---------- AC-10: a restart ----------


def test_ac10_a_job_left_running_is_queued_again(site):
    _, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1"])
    jobs._update(settings.db_path, job["id"], state="running")
    jobs.recover(settings.db_path)
    assert jobs.get(settings.db_path, job["id"])["state"] == "queued"


def test_ac10_the_document_in_flight_is_recorded_as_an_ambiguous_call(site):
    _, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1"])
    jobs._update(settings.db_path, job["id"], state="running", current_document="d1")
    jobs.recover(settings.db_path)
    [row] = _calls(settings)
    assert (row["outcome"], row["document_id"], row["job_id"]) == ("ambiguous", "d1", job["id"])


def test_ac10_a_recovered_job_drafts_only_what_is_still_pending(site, monkeypatch):
    app, settings, _ = site
    first = _drafted(_keyed(settings), "F001AAA")
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F004")
    with next_document_ids("F004AAA"):
        [second] = split.confirm_batch_split(batch, [{"display_id": "F004AAA", "sequence_indices": [0]}], settings)
    _service(monkeypatch, [_answer()])
    new_document.run_transcribe_new_documents(settings, NewDocumentOptions(max_total_cost=10), document_ids=[first])
    job = jobs.enqueue(settings.db_path, "draft", document_ids=[first, second])
    jobs._update(settings.db_path, job["id"], state="running")
    jobs.recover(settings.db_path)
    sent = _service(monkeypatch, [_answer()])
    jobs.run_and_wait(settings.db_path, app.state.job_handlers, jobs.get(settings.db_path, job["id"]))
    assert len(sent) == 1 and jobs.get(settings.db_path, job["id"])["state"] == "succeeded"


# ---------- AC-11: cancel, retry and account problems ----------


def test_ac11_a_queued_job_is_cancelled_at_once(site):
    app, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1"])
    assert signed_in(app, "paul").post(f"/api/jobs/{job['id']}/cancel").json()["state"] == "cancelled"


def test_ac11_a_running_job_stops_after_its_current_document(site):
    _, settings, _ = site
    done = []

    def handler(job, progress):
        for document_id in job["document_ids"]:
            progress(done=len(done), current=document_id)
            done.append(document_id)
            jobs.cancel(settings.db_path, job["id"])
        return {}
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f9", document_ids=["d1", "d2", "d3"])
    finished = jobs.run_and_wait(settings.db_path, {"draft": handler}, job)
    assert finished["state"] == "cancelled" and done == ["d1"]


def test_ac11_a_reviewer_cannot_cancel(site):
    app, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=["d1"])
    assert signed_in(app, "john").post(f"/api/jobs/{job['id']}/cancel").status_code == 403


def test_ac11_a_refused_key_fails_the_job_with_the_reason(site, monkeypatch):
    app, settings, _ = site
    _drafted(_keyed(settings))
    _service(monkeypatch, [FakeReply(401, {"error": {"message": "User not found"}})])
    with db.connect(settings.db_path) as conn:
        pending = db.get_new_pending_document_ids(conn)
    job = jobs.enqueue(settings.db_path, "draft", document_ids=pending)
    finished = jobs.run_and_wait(settings.db_path, app.state.job_handlers, job)
    assert (finished["state"], finished["error_kind"]) == ("failed", "account") and "401" in finished["error_text"]


def test_ac11_a_failed_job_can_be_retried(site):
    app, settings, _ = site
    job = jobs.enqueue(settings.db_path, "draft", folder_id="f1", document_ids=[])
    jobs._update(settings.db_path, job["id"], state="failed", error_kind="account", error_text="402")
    assert signed_in(app, "paul").post(f"/api/jobs/{job['id']}/retry").json()["state"] in ("queued", "running",
                                                                                          "succeeded")


# ---------- AC-12: the wizard ----------


def test_ac12_the_wizard_asks_for_the_model_right_after_the_prompt():
    steps = [key for key, _ in site_settings.SETUP_STEPS]
    assert steps.index("model") == steps.index("prompt") + 1


def test_ac12_a_passing_connection_test_completes_the_step(site, monkeypatch):
    app, _, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    _probe(monkeypatch, FakeReply(200, {"data": {"usage": 0}}))
    client.post("/api/models/test")
    steps = {s["id"]: s["done"] for s in client.get("/api/setup").json()["steps"]}
    assert steps["model"] is True


def test_ac8_a_reviewers_page_loses_the_models_and_usage_tabs():
    source = (ROOT / "app" / "static" / "workspace.js").read_text(encoding="utf-8")
    hidden = source[source.index('["settings", "deletions"'):].split("]")[0]
    assert '"models"' in hidden and '"usage"' in hidden


# ---------- regressions from the release review (2026-10-21) ----------


def _headers_sent(monkeypatch, replies):
    headers = []

    class Session:
        def post(self, url, **kwargs):
            headers.append(kwargs.get("headers") or {})
            return replies.pop(0)
    monkeypatch.setattr(openrouter, "_session_with_retries", lambda retries: Session())
    return headers


def test_switching_to_another_server_without_a_new_key_drops_the_old_key(site):
    """Release review P1: the OpenRouter key stayed and was sent to the new server."""
    app, _, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    shown = client.put("/api/models", json={"kind": "openai_compatible", "base_url": "http://10.0.0.7:8000/v1"}).json()
    assert shown["has_key"] is False


def test_a_new_server_address_drops_the_key_made_for_the_old_one(site):
    """Release review P1: changing the address alone also kept the key."""
    app, _, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"kind": "openai_compatible", "base_url": "http://10.0.0.7:8000/v1",
                                    "api_key": "local-key-000000"})
    shown = client.put("/api/models", json={"base_url": "http://10.0.0.8:8000/v1"}).json()
    assert shown["has_key"] is False


def test_the_old_key_is_never_sent_to_the_new_server(site, monkeypatch):
    """Release review P1: the Authorization header carried the OpenRouter key to the new URL."""
    app, settings, _ = site
    client = signed_in(app, "olive")
    client.put("/api/models", json={"api_key": "sk-or-v1-abcdef123456"})
    client.put("/api/models", json={"kind": "openai_compatible", "base_url": "http://10.0.0.7:8000/v1"})
    headers = _headers_sent(monkeypatch, [_answer()])
    _call(settings)
    assert "Authorization" not in headers[0]


def test_a_keyless_local_server_drafts(site, monkeypatch):
    """Release review P1: the test passed without a key but drafting refused to call."""
    app, settings, _ = site
    signed_in(app, "olive").put("/api/models", json={"kind": "openai_compatible", "base_url": "http://10.0.0.7:8000/v1"})
    _headers_sent(monkeypatch, [_answer(text="Dear Madam.")])
    assert _call(settings)["text"] == "Dear Madam."


def test_openrouter_without_a_key_is_still_refused_before_any_call(site, monkeypatch):
    _, settings, _ = site
    settings.openrouter_api_key = ""
    headers = _headers_sent(monkeypatch, [_answer()])
    with pytest.raises(openrouter.AccountError):
        _call(settings)
    assert headers == []


@pytest.mark.parametrize("amount", ["NaN", "nan", "inf", "-inf"])
def test_a_monthly_cap_that_is_not_a_number_is_refused(site, amount):
    """Release review P2: monthly_cap "NaN" was accepted and stored as no cap at all."""
    app, _, _ = site
    assert signed_in(app, "paul").put("/api/usage/caps", json={"monthly_cap": amount}).status_code == 400


def test_an_infinite_all_time_cap_is_refused(site):
    app, _, _ = site
    assert signed_in(app, "paul").put("/api/usage/caps", json={"max_total_cost": "inf"}).status_code == 400


def test_a_split_job_carries_its_batchs_folder(site, monkeypatch):
    """Release review P2: split jobs had no folder, so a reviewer of that folder could not see them."""
    app, settings, _ = site
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F005")
    monkeypatch.setattr(review_server, "run_propose_split", lambda batch_id, s: {0: True})
    proposal = signed_in(app, "olive").post(f"/api/new/batches/{batch}/retry-proposal").json()
    [job] = [j for j in jobs.listing(settings.db_path) if j["batch_id"] == batch]
    assert job["folder_id"] == proposal["folder_id"]


def test_a_reviewer_sees_the_split_job_of_a_folder_assigned_to_them(site, monkeypatch):
    app, settings, _ = site
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="F006")
    monkeypatch.setattr(review_server, "run_propose_split", lambda batch_id, s: {0: True})
    folder_id = signed_in(app, "olive").post(f"/api/new/batches/{batch}/retry-proposal").json()["folder_id"]
    with db.connect(settings.db_path) as conn:
        john = accounts.get_user_by_name(conn, "john")["id"]
    signed_in(app, "paul").post("/api/team/assign", json={"folder_id": folder_id, "reviewer_id": john, "workflow": "new"})
    [job] = [j for j in jobs.listing(settings.db_path) if j["batch_id"] == batch]
    assert signed_in(app, "john").get(f"/api/jobs/{job['id']}").status_code == 200


def test_a_billed_call_queued_at_exit_still_reaches_the_ledger(site):
    """Release review P1: a normal exit with a queued row left it out of the database."""
    import sys
    _, settings, _ = site
    script = ("import sys\n"
              f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
              "from incipit import ledger\n"
              f"ledger.record({settings.db_path!r}, purpose='draft', cost_usd=0.0042, cost_source='provider', outcome='ok')\n")
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and [r["cost_usd"] for r in _calls(settings)] == [0.0042]


def test_a_settings_panel_mounted_again_answers_each_click_once():
    """Release review P2: reopening a Settings panel added another click handler each time."""
    import shutil
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    kit = (ROOT / "app" / "static" / "page-kit.js").read_text(encoding="utf-8")
    harness = ("globalThis.window = globalThis;\n" + kit + "\n"
               "const panel = new EventTarget(); let clicks = 0;\n"
               "for (let opening = 0; opening < 3; opening += 1) {\n"
               "  const signal = window.pageKit.remount(panel);\n"
               "  panel.addEventListener('click', () => { clicks += 1; }, { signal });\n"
               "}\n"
               "panel.dispatchEvent(new Event('click'));\n"
               "console.log(clicks);\n")
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "1", done.stderr


@pytest.mark.parametrize("name", ["models.js", "backups.js", "access.js", "tabs_admin.js"])
def test_every_settings_panel_listens_through_its_mounts_signal(name):
    """Release review P2: the panels listened on the shared container directly."""
    source = (ROOT / "app" / "static" / name).read_text(encoding="utf-8")
    assert "container.addEventListener(type, handler, { signal })" in source and 'container.addEventListener("' not in source
