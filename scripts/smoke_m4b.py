#!/usr/bin/env python
"""M4b smoke: a client's install from an empty folder to a shared address.

Makes a data folder with `incipit init-folder`, starts `incipit serve` from it as a real process on
a free port, and walks the M4b checklist over HTTP: the wizard demands the token, the owner is made,
the earlier steps are finished, the Access page names the port and the folder, the port test finds
the host answering, the port is changed in Access and lands in the file, a reviewer is refused, the
data folder is moved from Access (the smoke plays the tray, starting the site again when it asks), and
the icon set is generated and checked. The router is never asked to forward anything.

    .venv/Scripts/python.exe scripts/smoke_m4b.py
"""

from __future__ import annotations

import hashlib
import os
import ssl
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import requests
import urllib3

# The site's own certificate is checked by its fingerprint below, not by a certificate authority.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_m0 import Checks  # noqa: E402

import build_icons  # noqa: E402

OWNER = ("olive", "olive-smoke-password")
REVIEWER = ("john", "john-smoke-password")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m4b-", ignore_cleanup_errors=True) as tmp:
        # Its own location file, and supervised, so a move hands the restart back to this script.
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INCIPIT_SETTINGS_DIR": str(Path(tmp) / "settings"),
               "INCIPIT_SUPERVISED": "1"}
        folder = Path(tmp) / "Incipit Data"
        port, https_port = free_port(), free_port()
        # The smoke later moves the site to port + 1, which must stay clear of the HTTPS port.
        while https_port in (port, port + 1):
            https_port = free_port()
        site_name = f"incipit-smoke-{port}"
        made = subprocess.run([sys.executable, "-m", "incipit.cli", "init-folder", str(folder), "--port", str(port),
                               "--site-name", site_name, "--https-port", str(https_port)],
                              capture_output=True, text=True, env=env)
        checks.ok("1 init-folder makes the folder", made.returncode == 0, made.stderr[-200:])
        layout = sorted(p.name for p in folder.iterdir())
        checks.ok("1 the folder holds the database, the file and every directory",
                  {"incipit.db", "incipit.toml", "bucket", "exports", "backups", "logs"} <= set(layout), str(layout))
        config = tomllib.loads((folder / "incipit.toml").read_text(encoding="utf-8"))
        checks.ok("1 the file names the port", config["server"]["port"] == port)
        token = next((line.split("token=")[1].strip() for line in made.stdout.splitlines() if "token=" in line), "")
        checks.ok("1 init-folder prints the setup link with its token", bool(token), made.stdout[-200:])

        def start():
            # No folder named: the site finds it through the location file, as the installed tray does.
            return subprocess.Popen([sys.executable, "-m", "incipit.cli", "serve"], cwd=tmp,
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def wait_up():
            # A minute: a restart after a move copies the folder first.
            for _ in range(300):
                try:
                    requests.get(f"{base}/health", timeout=1)
                    return True
                except requests.RequestException:
                    time.sleep(0.2)
            return False

        server = start()
        base = f"http://127.0.0.1:{port}"
        try:
            wait_up()
            anon = requests.Session()
            checks.ok("2 serve answers on the folder's port", anon.get(f"{base}/login").status_code == 200)
            checks.ok("2 the wizard demands its token", anon.get(f"{base}/setup").status_code == 404)
            owner = requests.Session()
            created = owner.post(f"{base}/api/setup/owner", json={"token": token, "username": OWNER[0],
                                                                  "password": OWNER[1], "display_name": "Olive"})
            checks.ok("2 the token creates the owner", created.status_code == 200, created.text[:200])
            steps = [s["id"] for s in owner.get(f"{base}/api/setup").json()["steps"]]
            checks.ok("2 the wizard's last step is access", steps[-1] == "access", str(steps))
            owner.post(f"{base}/api/team/users", json={"username": REVIEWER[0], "password": REVIEWER[1],
                                                        "display_name": "John", "role": "reviewer"})

            access = owner.get(f"{base}/api/access").json()
            checks.ok("3 Access names the port and the data folder",
                      access["port"] == port and Path(access["data_folder"]) == folder.resolve(), str(access)[:200])
            checks.ok("3 every shared link carries the port", all(f":{port}/" in link["link"] for link in access["links"]))
            for _ in range(40):
                if owner.get(f"{base}/api/access").json()["local_name"]["announcing"]:
                    break
                time.sleep(0.25)
            named = owner.get(f"{base}/api/access").json()
            checks.ok("3 the site announces its local name, listed first",
                      named["local_name"]["announcing"] and named["links"][0]["link"] == f"http://{site_name}.local:{port}/",
                      str(named["local_name"]))
            try:
                by_name = requests.get(f"http://{site_name}.local:{port}/login", timeout=5).status_code
            except requests.RequestException as exc:
                by_name = str(exc)[:80]
            checks.ok("3 the site opens by its .local name", by_name == 200, str(by_name))
            secure = named["https"]
            checks.ok("3 the site serves HTTPS on its own port", secure["serving"] and secure["port"] == https_port, str(secure)[:200])
            try:
                over_tls = requests.get(f"https://127.0.0.1:{https_port}/login", timeout=5, verify=False).status_code
            except requests.RequestException as exc:
                over_tls = str(exc)[:80]
            checks.ok("3 the sign-in page opens over HTTPS", over_tls == 200, str(over_tls))
            served = ssl.get_server_certificate(("127.0.0.1", https_port))
            digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(served)).hexdigest().upper()
            checks.ok("3 the certificate served is the one whose fingerprint Access shows",
                      ":".join(digest[i:i + 2] for i in range(0, 64, 2)) == secure["fingerprint"])
            checks.ok("3 anyone can download the certificate",
                      anon.get(f"{base}/certificate.crt").content.startswith(b"-----BEGIN CERTIFICATE-----"))
            tested = owner.post(f"{base}/api/access/test").json()["results"]
            checks.ok("3 the port test finds the site answering on this computer",
                      any(r["address"] == "localhost" and r["reachable"] for r in tested), str(tested)[:200])
            moved = owner.put(f"{base}/api/access", json={"port": port + 1}).json()
            on_disk = tomllib.loads((folder / "incipit.toml").read_text(encoding="utf-8"))["server"]["port"]
            checks.ok("3 a new port is written to the file for the next start",
                      moved.get("restart_needed") is True and on_disk == port + 1, str(moved)[:160])

            reviewer = requests.Session()
            reviewer.post(f"{base}/login", data={"username": REVIEWER[0], "password": REVIEWER[1]}, allow_redirects=False)
            checks.ok("4 a reviewer is refused the access page", reviewer.get(f"{base}/api/access").status_code == 403)
            checks.ok("4 a reviewer cannot change the port",
                      reviewer.put(f"{base}/api/access", json={"port": 9000}).status_code == 403)
            logs = list((folder / "logs").glob("incipit.log*"))
            checks.ok("4 the server writes its log into the data folder", bool(logs))
            checks.ok("4 a reviewer is refused the data folder's whereabouts",
                      reviewer.get(f"{base}/api/access/storage").status_code == 403)
            checks.ok("5 forwarding is off unless asked for", access["internet"]["state"] == "off", str(access["internet"]))

            storage = owner.get(f"{base}/api/access/storage").json()
            checks.ok("6 Access shows the folder, its size and the free space",
                      Path(storage["data_folder"]) == folder.resolve() and storage["size_bytes"] > 0 and storage["free_bytes"] > 0,
                      str(storage)[:200])
            busy = Path(tmp) / "Busy"
            busy.mkdir()
            (busy / "note.txt").write_text("x")
            refused = owner.post(f"{base}/api/access/move", json={"target": str(busy)})
            checks.ok("6 a folder with files in it is refused", refused.status_code == 400, refused.text[:120])
            # The port written earlier is the one the moved site starts on.
            base = f"http://127.0.0.1:{port + 1}"
            new_folder = Path(tmp) / "Moved Data"
            asked = owner.post(f"http://127.0.0.1:{port}/api/access/move", json={"target": str(new_folder)}).json()
            checks.ok("6 the move is accepted and the site restarts", asked.get("restarting") is True, str(asked)[:160])
            try:
                code = server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                code = None
            checks.ok("6 the site exits asking to be started again", code == 75, str(code))
            server = start()
            checks.ok("6 the site comes back", wait_up())
            again = requests.Session()
            again.post(f"{base}/login", data={"username": OWNER[0], "password": OWNER[1]}, allow_redirects=False)
            moved_to = again.get(f"{base}/api/access/storage").json()
            checks.ok("6 it runs from the new folder", Path(moved_to.get("data_folder", "")) == new_folder.resolve(),
                      str(moved_to)[:200])
            checks.ok("6 the team, documents and settings came with it",
                      any(u["username"] == REVIEWER[0] for u in again.get(f"{base}/api/team/users").json()["people"]))
            checks.ok("6 the old folder is kept with a note", (folder / "incipit.db").is_file() and (folder / "MOVED-TO.txt").is_file())
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()

        icons = Path(tmp) / "icons"
        build_icons.build(out_dir=icons)
        problems = build_icons.check(icons)
        checks.ok("7 the icon set is complete: every size, square, not blank, and an .ico", not problems, "; ".join(problems))

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
