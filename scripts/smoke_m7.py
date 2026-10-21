#!/usr/bin/env python
"""M7 smoke: updates from a release feed, and the support bundle, against a real site.

Serves a release feed and an "installer" from a local server (the installer is a copy of Windows'
own whoami.exe, harmless whatever arguments it is given), starts a site from a fresh data folder
that reads that feed, and as the owner: checks for updates and sees the newer version and its notes;
has an install refused when the feed's checksum is wrong, with nothing left on disk; downloads a
support bundle and finds the versions, no database, and a key planted in the log masked; and finally
installs, seeing the site download, check and start the installer and stop with the update exit
code. An admin sees the update but cannot install it; a reviewer is refused both pages.

    .venv/Scripts/python.exe scripts/smoke_m7.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_m0 import Checks  # noqa: E402

OWNER = ("olive", "olive-smoke-password")
ADMIN = ("paul", "paul-smoke-password")
REVIEWER = ("john", "john-smoke-password")
INSTALLER = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
PLANTED = "sk-or-v1-plantedsecret0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Releases(BaseHTTPRequestHandler):
    """latest.json and the installer it names; `wrong_checksum` makes the feed lie."""

    wrong_checksum = False
    port = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = INSTALLER.read_bytes()
        if self.path.endswith(".exe"):
            data, kind = body, "application/octet-stream"
        else:
            digest = "0" * 64 if Releases.wrong_checksum else hashlib.sha256(body).hexdigest()
            data = json.dumps({"version": "9.9.0", "date": "2026-11-02", "notes": "Faster drafting.\nA new tray.",
                               "downloads": {"windows": {"url": f"http://127.0.0.1:{Releases.port}/Incipit-9.9.0-setup.exe",
                                                         "size": len(body), "sha256": digest}}}).encode()
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    checks = Checks()
    Releases.port = free_port()
    releases = ThreadingHTTPServer(("127.0.0.1", Releases.port), Releases)
    threading.Thread(target=releases.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m7-", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        settings_dir = tmp / "settings"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INCIPIT_SETTINGS_DIR": str(settings_dir),
               "INCIPIT_SUPERVISED": "1", "INCIPIT_UPDATE_FEED": f"http://127.0.0.1:{Releases.port}/latest.json"}
        folder = tmp / "Incipit Data"
        port = free_port()
        made = subprocess.run([sys.executable, "-m", "incipit.cli", "init-folder", str(folder), "--port", str(port),
                               "--https-port", str(free_port())], capture_output=True, text=True, env=env)
        token = next((line.split("token=")[1].strip() for line in made.stdout.splitlines() if "token=" in line), "")
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, "-m", "incipit.cli", "serve", "--data-folder", str(folder)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def login(user):
            s = requests.Session()
            s.post(f"{base}/login", data={"username": user[0], "password": user[1]}, allow_redirects=False)
            return s

        try:
            for _ in range(300):
                try:
                    requests.get(f"{base}/health", timeout=1)
                    break
                except requests.RequestException:
                    time.sleep(0.2)
            owner = requests.Session()
            owner.post(f"{base}/api/setup/owner", json={"token": token, "username": OWNER[0], "password": OWNER[1],
                                                        "display_name": "Olive"})
            for (name, password), role in ((ADMIN, "admin"), (REVIEWER, "reviewer")):
                owner.post(f"{base}/api/team/users", json={"username": name, "password": "temporary-password-1",
                                                            "display_name": name.title(), "role": role})
                login((name, "temporary-password-1")).post(f"{base}/api/me/password",
                                                           json={"current": "temporary-password-1", "new": password})

            found = owner.post(f"{base}/api/updates/check").json()
            checks.ok("1 the check finds the newer version with its notes",
                      found.get("state") == "available" and found["release"]["version"] == "9.9.0"
                      and "Faster drafting" in found["release"]["notes"], str(found)[:200])
            checks.ok("1 the page names the version running", found.get("current") == (ROOT / "VERSION").read_text().strip())

            Releases.wrong_checksum = True
            refused = owner.post(f"{base}/api/updates/install")
            left = list((settings_dir / "updates").glob("*")) if (settings_dir / "updates").is_dir() else []
            checks.ok("2 an installer that does not match the feed is refused", refused.status_code == 400
                      and "did not match" in refused.text, refused.text[:160])
            checks.ok("2 and nothing of it is left on disk", left == [], str(left))
            checks.ok("2 the site keeps running", requests.get(f"{base}/health", timeout=5).status_code in (200, 503))
            Releases.wrong_checksum = False

            paul = login(ADMIN)
            checks.ok("3 an admin sees the update", paul.get(f"{base}/api/updates").json().get("state") == "available")
            checks.ok("3 but cannot install it", paul.post(f"{base}/api/updates/install").status_code == 403)
            john = login(REVIEWER)
            checks.ok("3 a reviewer is refused updates and the support bundle",
                      (john.get(f"{base}/api/updates").status_code, john.get(f"{base}/api/support/bundle").status_code) == (403, 403))

            with (folder / "logs" / "incipit.log").open("a", encoding="utf-8") as log:
                log.write(f"2026-10-21 12:00:00 WARNING smoke: a key that should never leave: {PLANTED}\n")
            got = paul.get(f"{base}/api/support/bundle")
            bundle = zipfile.ZipFile(io.BytesIO(got.content))
            names = bundle.namelist()
            logs = "".join(bundle.read(n).decode("utf-8", errors="replace") for n in names if n.startswith("logs/"))
            checks.ok("4 an admin downloads a support bundle", got.status_code == 200 and "versions.json" in names, str(names))
            checks.ok("4 it holds no database and no scans", not any(n.endswith((".db", ".jpg")) for n in names), str(names))
            checks.ok("4 a key planted in the log is masked", PLANTED not in logs and "masked" in logs)
            checks.ok("4 the versions are named", "incipit" in json.loads(bundle.read("versions.json")))

            started = owner.post(f"{base}/api/updates/install").json()
            checks.ok("5 installing downloads and checks the installer", started.get("installing") == "9.9.0", str(started)[:200])
            installed = list((settings_dir / "updates").glob("Incipit-9.9.0-setup.exe"))
            checks.ok("5 the checked installer is kept in this computer's own settings folder",
                      len(installed) == 1 and installed[0].read_bytes() == INSTALLER.read_bytes())
            try:
                code = server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                code = None
            checks.ok("5 the site stops for the installer with the update exit code", code == 76, str(code))
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=15)
            releases.shutdown()

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
