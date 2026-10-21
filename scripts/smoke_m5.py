#!/usr/bin/env python
"""M5 smoke: the model service connected from the Models page, every call in the ledger, the caps,
and jobs that finish after the site is killed mid-draft.

Starts a fake model service (an OpenAI-compatible server on a free local port) and a real site from
a fresh data folder, then as the owner: saves the key and sees only its hint, tests the connection,
uploads drawn pages and proposes a split (a job), confirms and drafts (a job), reads the calls on the
Usage page, deletes a document and sees spend unchanged, sets a monthly cap below spend and sees the
next draft stop with the cap's message, then kills the site while a draft is in flight, starts it
again and sees the job finish with no document drafted twice. A reviewer is refused Models and Usage.

    .venv/Scripts/python.exe scripts/smoke_m5.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_m0 import Checks, drawn_page  # noqa: E402

OWNER = ("olive", "olive-smoke-password")
REVIEWER = ("john", "john-smoke-password")
DRAFT_COST, SPLIT_COST = 0.0021, 0.001


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeService(BaseHTTPRequestHandler):
    """Answers like a vision model: a split proposal or a drafted document, with its cost in usage."""

    slow_seconds = 0.0
    received = []

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "fake-vision"}]})
        else:
            self._send(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = payload["messages"][0]["content"]
        images = sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image_url")
        kind = (payload.get("response_format") or {}).get("json_schema", {}).get("name", "")
        FakeService.received.append(kind)
        if kind == "split":
            answer = {"starts_new_document": [i % 2 == 0 for i in range(images)],
                      "is_duplicate_of_previous_page": [False] * images}
            cost = SPLIT_COST
        else:
            time.sleep(FakeService.slow_seconds)
            text = "\n".join(f"[page {n + 1}]\nDear Sir, line {n + 1} of the letter." for n in range(images))
            answer = {"text": text, "uncertain_words": [], "document_type": "Letter", "author": "Hayes, Howard"}
            cost = DRAFT_COST
        self._send(200, {"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 900 * max(images, 1), "completion_tokens": 120, "cost": cost}})


def calls(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM model_calls ORDER BY id")]
    finally:
        conn.close()


def main() -> int:
    checks = Checks()
    service_port = free_port()
    service = ThreadingHTTPServer(("127.0.0.1", service_port), FakeService)
    threading.Thread(target=service.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m5-", ignore_cleanup_errors=True) as tmp:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INCIPIT_SETTINGS_DIR": str(Path(tmp) / "settings")}
        env.pop("OPENROUTER_API_KEY", None)
        folder = Path(tmp) / "Incipit Data"
        port, https_port = free_port(), free_port()
        made = subprocess.run([sys.executable, "-m", "incipit.cli", "init-folder", str(folder), "--port", str(port),
                               "--https-port", str(https_port)], capture_output=True, text=True, env=env)
        token = next((line.split("token=")[1].strip() for line in made.stdout.splitlines() if "token=" in line), "")
        base = f"http://127.0.0.1:{port}"
        db_path = folder / "incipit.db"

        def start():
            return subprocess.Popen([sys.executable, "-m", "incipit.cli", "serve", "--data-folder", str(folder)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def wait_up():
            for _ in range(300):
                try:
                    requests.get(f"{base}/health", timeout=1)
                    return True
                except requests.RequestException:
                    time.sleep(0.2)
            return False

        def login(user):
            s = requests.Session()
            s.post(f"{base}/login", data={"username": user[0], "password": user[1]}, allow_redirects=False)
            return s

        def upload(owner, display_id, pages):
            files = [("files", (f"{display_id}_Page_{i + 1:02d}.jpg", drawn_page(i + len(display_id)), "image/jpeg"))
                     for i in range(pages)]
            batch = owner.post(f"{base}/api/new/batches", files=files, data={"folder_display_id": display_id}).json()
            proposal = owner.post(f"{base}/api/new/batches/{batch['batch_id']}/retry-proposal").json()
            return batch["batch_id"], proposal

        def groups(display_id, pages):
            return [{"display_id": f"{display_id}A{chr(65 + n // 2)}{chr(65 + n // 2)}"[:len(display_id) + 3],
                     "sequence_indices": [n, n + 1]} for n in range(0, pages, 2)]

        server = start()
        try:
            wait_up()
            owner = requests.Session()
            created = owner.post(f"{base}/api/setup/owner", json={"token": token, "username": OWNER[0],
                                                                  "password": OWNER[1], "display_name": "Olive"})
            checks.ok("1 the owner is made", created.status_code == 200, created.text[:200])
            owner.post(f"{base}/api/team/users", json={"username": REVIEWER[0], "password": REVIEWER[1],
                                                        "display_name": "John", "role": "reviewer"})

            saved = owner.put(f"{base}/api/models", json={
                "kind": "openai_compatible", "base_url": f"http://127.0.0.1:{service_port}/v1",
                "api_key": "smoke-secret-key-7788", "model": "fake-vision", "fallback_model": ""})
            checks.ok("1 the key is saved and only its hint comes back",
                      saved.json().get("key_hint") == "…7788" and "smoke-secret" not in saved.text, saved.text[:200])
            tested = owner.post(f"{base}/api/models/test").json()
            checks.ok("1 the connection test passes without a chargeable call",
                      tested.get("ok") and "split" not in FakeService.received and not FakeService.received,
                      str(tested)[:160])
            raw = sqlite3.connect(db_path).execute("SELECT api_key_enc FROM model_backends").fetchone()[0]
            checks.ok("1 the database holds the key only encrypted", raw and "smoke-secret" not in raw)

            batch, proposal = upload(owner, "SM51", 4)
            starts = [p["proposed_new_document"] for p in proposal.get("pages", [])]
            checks.ok("2 the split is proposed by the model", starts == [True, False, True, False], str(starts)[:120])
            split_jobs = [j for j in owner.get(f"{base}/api/jobs").json()["jobs"] if j["batch_id"] == batch]
            checks.ok("2 the proposal ran as a job that succeeded",
                      [j["state"] for j in split_jobs] == ["succeeded"], str(split_jobs)[:200])
            confirmed = owner.post(f"{base}/api/new/batches/{batch}/confirm",
                                   json={"groups": groups("SM51", 4)}).json()
            folder_id = proposal["folder_id"]
            checks.ok("2 confirming drafts both documents", len(confirmed.get("created", [])) == 2
                      and not confirmed.get("error"), str(confirmed)[:200])
            draft_jobs = [j for j in owner.get(f"{base}/api/jobs?folder_id={folder_id}").json()["jobs"] if j["kind"] == "draft"]
            checks.ok("2 the drafting ran as a job: 2 of 2 done",
                      [(j["state"], j["done"], j["total"]) for j in draft_jobs] == [("succeeded", 2, 2)], str(draft_jobs)[:200])

            usage = owner.get(f"{base}/api/usage").json()
            expected = 2 * DRAFT_COST + SPLIT_COST
            checks.ok("3 the Usage page counts every call", abs(usage["all_time"] - expected) < 1e-9,
                      f"{usage['all_time']} vs {expected}")
            purposes = {r["name"] for r in usage["by_purpose"]}
            checks.ok("3 spend is split by purpose and by model",
                      purposes == {"draft", "split"} and usage["by_model"][0]["name"] == "fake-vision", str(purposes))
            rows = calls(db_path)
            checks.ok("3 each draft call names its job and document",
                      all(r["job_id"] and r["document_id"] for r in rows if r["purpose"] == "draft"), str(rows)[:200])
            deleted = owner.delete(f"{base}/api/new/documents/{confirmed['created'][0]}?reason=smoke+test")
            after = owner.get(f"{base}/api/usage").json()["all_time"]
            checks.ok("3 deleting a drafted document leaves spend as it was",
                      deleted.status_code == 200 and abs(after - expected) < 1e-9, f"{deleted.status_code} {after}")

            owner.put(f"{base}/api/usage/caps", json={"monthly_cap": expected / 2})
            batch2, _ = upload(owner, "SM52", 2)
            stopped = owner.post(f"{base}/api/new/batches/{batch2}/confirm", json={"groups": groups("SM52", 2)}).json()
            checks.ok("4 a monthly cap below spend stops the next draft with the cap's message",
                      "monthly cap" in (stopped.get("error") or ""), str(stopped)[:200])
            owner.put(f"{base}/api/usage/caps", json={"monthly_cap": ""})

            # Kill the site while the model is still working on a document, then start it again.
            FakeService.slow_seconds = 6
            batch3, proposal3 = upload(owner, "SM53", 4)
            folder3 = proposal3["folder_id"]
            def confirm_then_lose_the_site():
                try:
                    requests.post(f"{base}/api/new/batches/{batch3}/confirm", json={"groups": groups("SM53", 4)},
                                  cookies=owner.cookies, timeout=60)
                except requests.RequestException:
                    pass  # the site is killed under this request on purpose
            threading.Thread(target=confirm_then_lose_the_site, daemon=True).start()
            for _ in range(100):
                running = [j for j in owner.get(f"{base}/api/jobs?folder_id={folder3}").json()["jobs"] if j["kind"] == "draft"]
                if running and running[0]["current_document"]:
                    break
                time.sleep(0.1)
            server.kill()
            server.wait(timeout=15)
            FakeService.slow_seconds = 0
            server = start()
            checks.ok("5 the site comes back after being killed mid-draft", wait_up())
            owner = login(OWNER)
            finished = []
            for _ in range(200):
                finished = [j for j in owner.get(f"{base}/api/jobs?folder_id={folder3}").json()["jobs"] if j["kind"] == "draft"]
                if finished and finished[0]["state"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.25)
            checks.ok("5 the interrupted job finishes after the restart",
                      finished and finished[0]["state"] == "succeeded" and finished[0]["done"] == 2, str(finished)[:200])
            rows = [r for r in calls(db_path) if r["purpose"] == "draft"]
            folder3_docs = {r["document_id"] for r in rows if r["job_id"] == finished[0]["id"]}
            oks = [r["document_id"] for r in rows if r["job_id"] == finished[0]["id"] and r["outcome"] == "ok"]
            checks.ok("5 no document of that job was drafted twice", len(oks) == len(set(oks)) == 2, str(oks))
            checks.ok("5 the call lost in the kill is recorded as ambiguous, with an estimate",
                      any(r["outcome"] == "ambiguous" and r["cost_source"] == "estimated" for r in rows
                          if r["document_id"] in folder3_docs), str([r["outcome"] for r in rows]))

            john = login(REVIEWER)
            checks.ok("6 a reviewer is refused the Models and Usage pages",
                      (john.get(f"{base}/api/models").status_code, john.get(f"{base}/api/usage").status_code) == (403, 403))
            checks.ok("6 a reviewer cannot cancel a job",
                      john.post(f"{base}/api/jobs/{finished[0]['id']}/cancel").status_code == 403)
        finally:
            server.kill()
            server.wait(timeout=15)
            service.shutdown()

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
