#!/usr/bin/env python
"""M2 smoke: a fresh site set up by its owner, branded, joined by an admin and a reviewer, then used.

Serves a real app on a throwaway database and bucket that has no owner yet, only a bootstrap token,
and walks the M2 checklist over HTTP: setup with and without the token, branding and an album
backdrop, join codes and who may make them, a password reset that ends a session, the org id and
the join switch, the wizard's finish, and finally the M0 journey with the new roles.

    .venv/Scripts/python.exe scripts/smoke_m2.py [--port 8099]
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
import uvicorn
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_m0 import ADMIN, REVIEWER, Checks, fake_call_openrouter, journey, session  # noqa: E402

from incipit import openrouter, review_server, site_settings  # noqa: E402
from incipit.config import Settings  # noqa: E402

OWNER = ("olive", "olive-smoke-password")


def picture(hue: int) -> bytes:
    image = Image.new("RGB", (640, 420), (hue, 120, 255 - hue))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def serve(port: int, root: Path) -> tuple[Settings, str]:
    """A site with no owner, only the token `incipit init` would have written."""
    settings = Settings(db_path=str(root / "smoke.db"), bucket_uri=(root / "bucket").as_uri(),
                        openrouter_api_key="sk-fake")
    openrouter.call_openrouter = fake_call_openrouter
    app = review_server.create_app(settings, str(root / "exports"), str(root / "logs"))
    token = site_settings.write_bootstrap_token(settings.db_path)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.1)
    return settings, token


def signup(base: str, username: str, password: str, code: str) -> requests.Session:
    s = requests.Session()
    s.post(f"{base}/signup", data={"username": username, "password": password,
                                   "display_name": username.title(), "join_code": code}, allow_redirects=False)
    return s


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m2-") as tmp:
        root = Path(tmp)
        settings, token = serve(args.port, root)
        anon = requests.Session()

        # ---------- 1 the bootstrap ----------
        checks.ok("1 /setup without the token is not found", anon.get(f"{base}/setup").status_code == 404)
        checks.ok("1 /setup with the token opens", anon.get(f"{base}/setup?token={token}").status_code == 200)
        owner = requests.Session()
        made = owner.post(f"{base}/api/setup/owner", json={"token": token, "username": OWNER[0],
                                                           "password": OWNER[1], "display_name": "Olive"})
        checks.ok("1 the token creates the owner", made.status_code == 200, made.text[:200])
        again = anon.post(f"{base}/api/setup/owner", json={"token": token, "username": "other",
                                                           "password": "other-password"})
        checks.ok("1 the token does not work twice", again.status_code == 404)
        checks.ok("1 the token file is gone", not site_settings.bootstrap_path(settings.db_path).exists())
        checks.ok("1 the unfinished owner is sent to the wizard",
                  owner.get(f"{base}/", allow_redirects=False).headers.get("location") == "/setup")

        # ---------- 2 the site's name and look ----------
        owner.put(f"{base}/api/site/settings", json={"product_name": "Marlowe Letters",
                                                     "role_label_reviewer": "Volunteer"})
        logo = owner.post(f"{base}/api/site/assets", data={"kind": "logo"},
                          files={"file": ("logo.png", picture(30), "image/png")}).json()
        album = [owner.post(f"{base}/api/site/assets", data={"kind": "album_image"},
                            files={"file": (f"p{i}.png", picture(60 * i), "image/png")}).json()["id"]
                 for i in range(3)]
        saved = owner.put(f"{base}/api/site/settings", json={"appearance": {
            "logo_asset": logo["id"], "accent_light": "#7a3b12", "accent_dark": "#e7a46a",
            "backdrop": {"kind": "album", "album": {"assets": album}}}})
        checks.ok("2 the owner saves a logo, an accent and a three-photo album", saved.status_code == 200,
                  saved.text[:200])
        login_page = anon.get(f"{base}/login").text
        checks.ok("2 the sign-in page carries the name and the logo",
                  "Marlowe Letters" in login_page and f"/site/assets/{logo['id']}" in login_page)
        checks.ok("2 /site.css carries the accent", "--accent: #7a3b12;" in anon.get(f"{base}/site.css").text)
        checks.ok("2 the public payload names the album's three photographs",
                  len(anon.get(f"{base}/api/site").json()["album"]) == 3)
        checks.ok("2 the album's photographs are served",
                  all(anon.get(f"{base}/site/assets/{i}").status_code == 200 for i in album))

        # ---------- 3 join codes ----------
        admin_code = owner.post(f"{base}/api/team/join-codes", json={"role": "admin"}).json()["code"]
        admin = signup(base, ADMIN[0], ADMIN[1], admin_code)
        checks.ok("3 an admin joins with the owner's admin code", admin.get(f"{base}/api/me").json().get("role") == "admin")
        refused = admin.post(f"{base}/api/team/join-codes", json={"role": "admin"})
        checks.ok("3 the admin is refused an admin code", refused.status_code == 403)
        reviewer_code = admin.post(f"{base}/api/team/join-codes", json={"role": "reviewer"}).json().get("code", "")
        checks.ok("3 the admin makes a reviewer code", bool(reviewer_code))
        checks.ok("3 /login?join=1 opens the form", 'show("signup");' in anon.get(f"{base}/login?join=1").text)
        reviewer = signup(base, REVIEWER[0], "john-first-password", reviewer_code)
        checks.ok("3 a reviewer joins with the admin's code",
                  reviewer.get(f"{base}/api/me").json().get("role") == "reviewer")

        # ---------- 4 a reset ends a session ----------
        people = {p["username"]: p["id"] for p in admin.get(f"{base}/api/team/users").json()["people"]}
        admin.post(f"{base}/api/team/users/{people[REVIEWER[0]]}/reset-password", json={"password": "temporary-1"})
        checks.ok("4 the reviewer's old session stops working", reviewer.get(f"{base}/api/me").status_code == 401)
        temporary = session(base, (REVIEWER[0], "temporary-1"))
        blocked = temporary.get(f"{base}/api/new/folders")
        checks.ok("4 a reset account can only change its password",
                  blocked.status_code == 403 and blocked.json().get("code") == "must_change_password")
        changed = temporary.post(f"{base}/api/me/password", json={"current": "temporary-1", "new": REVIEWER[1]})
        checks.ok("4 the reviewer chooses their own password", changed.status_code == 200)
        reviewer = session(base, REVIEWER)
        checks.ok("4 the reviewer signs in with it", reviewer.get(f"{base}/api/new/folders").status_code == 200)
        mine = reviewer.get(f"{base}/api/site/settings")
        checks.ok("4 the reviewer is refused the site settings", mine.status_code == 403)

        # ---------- 5 the org id and the join switch ----------
        before = owner.get(f"{base}/api/site/settings").json()["org_id"]
        after = owner.post(f"{base}/api/site/org-id").json()["org_id"]
        checks.ok("5 the owner regenerates the org id", after != before and after.startswith("ORG-"))
        checks.ok("5 the org id is not public", after not in anon.get(f"{base}/api/site").text)
        owner.put(f"{base}/api/site/settings", json={"join_enabled": False})
        checks.ok("5 with joining off the join link says so",
                  "Joining this site is closed" in anon.get(f"{base}/login?join=1").text)
        owner.put(f"{base}/api/site/settings", json={"join_enabled": True})

        # ---------- 6 the wizard finishes ----------
        early = owner.post(f"{base}/api/setup/finish")
        checks.ok("6 the wizard will not finish with steps left", early.status_code == 409)
        for step in ("site", "prompt", "model", "tabs", "people", "access"):
            owner.post(f"{base}/api/setup/steps/{step}", json={"done": True})
        checks.ok("6 the wizard finishes", bool(owner.post(f"{base}/api/setup/finish").json().get("completed_at")))
        checks.ok("6 the owner now lands on the site", owner.get(f"{base}/", allow_redirects=False).status_code == 200)

        # ---------- 7 the M0 journey, with the new roles ----------
        journey(base, checks, root, label="with M2 roles")

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
