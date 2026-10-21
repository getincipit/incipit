#!/usr/bin/env python
"""M6 smoke: backups to another folder, the manifest, and a restore from the command line and from
Settings, against a real site run from a data folder.

Runs a fake model service and the site, takes one folder through the whole M0 journey (upload,
split, draft, review, send back, approve, export), points backups at a folder outside the data
folder (a missing drive is refused), backs up now, deletes one scan from the backup, restores into a
new folder with `incipit restore` and serves it, then restores from Settings and sees the site come
back from the restored folder with the old one untouched.

    .venv/Scripts/python.exe scripts/smoke_m6.py
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
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

from smoke_m0 import ADMIN, REVIEWER, Checks, fake_call_openrouter, journey  # noqa: E402

OWNER = ("olive", "olive-smoke-password")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeService(BaseHTTPRequestHandler):
    """The M0 smoke's fake model, spoken over HTTP like an OpenAI-compatible server."""

    def log_message(self, *args):
        pass

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "fake-vision"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        parsed, usage = fake_call_openrouter(payload["messages"], None, schema=payload.get("response_format"),
                                             extract_result=lambda p, u: (p, u))
        self._send({"choices": [{"message": {"content": json.dumps(parsed)}, "finish_reason": "stop"}], "usage": usage})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    checks = Checks()
    service_port = free_port()
    service = ThreadingHTTPServer(("127.0.0.1", service_port), FakeService)
    threading.Thread(target=service.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m6-", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INCIPIT_SETTINGS_DIR": str(tmp / "settings"),
               "INCIPIT_SUPERVISED": "1"}
        env.pop("OPENROUTER_API_KEY", None)
        folder = tmp / "Incipit Data"
        port = free_port()
        made = subprocess.run([sys.executable, "-m", "incipit.cli", "init-folder", str(folder), "--port", str(port),
                               "--https-port", str(free_port())], capture_output=True, text=True, env=env)
        token = next((line.split("token=")[1].strip() for line in made.stdout.splitlines() if "token=" in line), "")
        base = f"http://127.0.0.1:{port}"

        def start(data_folder=None):
            return subprocess.Popen([sys.executable, "-m", "incipit.cli", "serve",
                                     *(["--data-folder", str(data_folder)] if data_folder else [])],
                                    env=env, cwd=str(tmp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop(process):
            process.kill()
            process.wait(timeout=15)

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

        server = start(folder)
        try:
            wait_up()
            owner = requests.Session()
            owner.post(f"{base}/api/setup/owner", json={"token": token, "username": OWNER[0], "password": OWNER[1],
                                                        "display_name": "Olive"})
            for (name, password), role, shown in ((ADMIN, "admin", "Paul"), (REVIEWER, "reviewer", "John")):
                # Made with a temporary password, which each person replaces at their first sign-in.
                owner.post(f"{base}/api/team/users", json={"username": name, "password": "temporary-password-1",
                                                            "display_name": shown, "role": role})
                first_sign_in = login((name, "temporary-password-1"))
                first_sign_in.post(f"{base}/api/me/password", json={"current": "temporary-password-1", "new": password})
            owner.put(f"{base}/api/models", json={"kind": "openai_compatible", "api_key": "smoke-key-123456",
                                                  "base_url": f"http://127.0.0.1:{service_port}/v1", "model": "fake-vision"})
            done = journey(base, checks, folder, db_name="incipit.db")

            missing_drive = owner.put(f"{base}/api/backups", json={"destination": "Q:\\Nowhere\\Backups"})
            checks.ok("7 a destination on a missing drive is refused", missing_drive.status_code == 400,
                      missing_drive.text[:160])
            destination = tmp / "Backups elsewhere"
            moved = owner.put(f"{base}/api/backups", json={"destination": str(destination)}).json()
            checks.ok("7 backups now go to a folder outside the data folder",
                      Path(moved["destination"]) == destination, str(moved)[:200])
            owner.post(f"{base}/api/backups/run")
            state = {}
            for _ in range(200):
                state = owner.get(f"{base}/api/backups").json()
                if not state["running"] and state["snapshots"]:
                    break
                time.sleep(0.2)
            checks.ok("7 Back up now makes a snapshot with a manifest",
                      state["snapshots"] and state["snapshots"][0]["manifest"] and state["last"]["ok"], str(state)[:240])

            first = done["documents"][0]
            scans = sorted((destination / "bucket" / "documents" / done["folder_id"] / first).glob("*"))
            checks.ok("7 the scans are in the backup, copied from inside the data folder", bool(scans),
                      str(sorted(p.name for p in tmp.iterdir())))
            scans[0].unlink()
            restored = tmp / "Restored by command"
            ran = subprocess.run([sys.executable, "-m", "incipit.cli", "restore", "--from", str(destination),
                                  str(restored)], capture_output=True, text=True, env=env)
            checks.ok("8 incipit restore names the scan missing from the backup and its document",
                      ran.returncode == 1 and scans[0].name in ran.stdout and "SM01AAA" in ran.stdout, ran.stdout[-300:])

            stop(server)
            server = start(restored)
            checks.ok("8 the restored folder serves", wait_up())
            admin = login(ADMIN)
            second = done["documents"][1]
            detail = admin.get(f"{base}/api/new/documents/{second}").json()
            checks.ok("8 its documents open with their reviewed text",
                      detail.get("transcription", {}).get("text") == "Admin accepted.", str(detail)[:200])
            checks.ok("8 their pages are served", admin.get(f"{base}/api/new/documents/{second}/pages/0/image").status_code == 200)
            comments = admin.get(f"{base}/api/documents/{first}/comments?workflow=new").json()
            checks.ok("8 the comments came back", "is page two right?" in json.dumps(comments))
            checks.ok("8 the exported files came back", bool(list((restored / "exports").glob(f"*/{done['folder_id']}/*.csv"))))
            stop(server)

            server = start(folder)
            wait_up()
            owner = login(OWNER)
            before = digest(folder / "incipit.db")
            snapshot = owner.get(f"{base}/api/backups").json()["snapshots"][0]["name"]
            again = tmp / "Restored from Settings"
            asked = owner.post(f"{base}/api/backups/restore", json={"snapshot": snapshot, "target": str(again)}).json()
            checks.ok("9 the owner asks for a restore and the site restarts", asked.get("restarting") is True, str(asked)[:200])
            try:
                code = server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                code = None
            checks.ok("9 the site exits to be started again", code == 75, str(code))
            server = start(folder)
            checks.ok("9 the site comes back", wait_up())
            owner = login(OWNER)
            now = owner.get(f"{base}/api/backups").json()
            checks.ok("9 it runs from the restored folder", Path(now.get("data_folder", "")) == again.resolve(), str(now)[:200])
            checks.ok("9 the old data folder is untouched", digest(folder / "incipit.db") == before)
            checks.ok("10 a reviewer is refused Backups", login(REVIEWER).get(f"{base}/api/backups").status_code == 403)
        finally:
            stop(server)
            service.shutdown()

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
