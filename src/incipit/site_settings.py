"""The site's own settings: its name, role labels, org id, join rules, appearance and setup progress."""

from __future__ import annotations

import sqlite3
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import accounts, appearance, site_assets
from .config import DEFAULT_PRODUCT_NAME

SCHEMA = """
-- One row, id 1. What the site calls itself and how people join it.
CREATE TABLE IF NOT EXISTS site_settings (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    product_name          TEXT NOT NULL,
    company_name          TEXT NOT NULL DEFAULT '',
    role_label_owner      TEXT NOT NULL DEFAULT 'Owner',
    role_label_admin      TEXT NOT NULL DEFAULT 'Admin',
    role_label_reviewer   TEXT NOT NULL DEFAULT 'Reviewer',
    time_zone             TEXT NOT NULL DEFAULT 'UTC',
    reviewer_self_signup  INTEGER NOT NULL DEFAULT 0,
    -- Typed on the hub's "Join an organisation" page to find this site; regenerating retires it.
    org_id                TEXT NOT NULL,
    join_enabled          INTEGER NOT NULL DEFAULT 1,
    appearance_json       TEXT NOT NULL DEFAULT '{}',
    setup_steps_done_json TEXT NOT NULL DEFAULT '[]',
    setup_completed_at    TEXT
);
"""

# The file `incipit init` writes when no owner was named; holding it is what proves the right to
# create the owner.
BOOTSTRAP_FILENAME = "bootstrap.token"

# The wizard's steps that exist so far, in order; later modules add theirs here.
SETUP_STEPS = (
    ("owner", "Create the owner account"),
    ("site", "Name the site and choose its look"),
    ("prompt", "Describe the collection for the model"),
    ("model", "Connect the transcription model"),
    ("tabs", "Set up the tabs"),
    ("people", "Invite your team"),
    ("access", "Share the address with your team"),
)

_ORG_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Fields an admin may change; the rest are the owner's alone.
ADMIN_FIELDS = ("product_name", "company_name", "role_label_owner", "role_label_admin",
                "role_label_reviewer", "time_zone", "reviewer_self_signup")
OWNER_FIELDS = ("join_enabled",)
TEXT_LIMITS = {"product_name": 60, "company_name": 120, "role_label_owner": 30,
               "role_label_admin": 30, "role_label_reviewer": 30, "time_zone": 64}


class SettingsError(ValueError):
    """A setting the person can see and fix."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_org_id() -> str:
    """`ORG-XXXX-XXXX`: short enough to read aloud, without the letters people confuse."""
    return "ORG-" + "-".join("".join(secrets.choice(_ORG_ALPHABET) for _ in range(4)) for _ in range(2))


def init_site(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO site_settings (id, product_name, org_id) VALUES (1, ?, ?) ON CONFLICT DO NOTHING",
                 (DEFAULT_PRODUCT_NAME, new_org_id()))


def settings(conn: sqlite3.Connection) -> dict:
    """Every setting, with appearance parsed and filled from its defaults."""
    row = conn.execute("SELECT * FROM site_settings WHERE id = 1").fetchone()
    if row is None:
        init_site(conn)
        row = conn.execute("SELECT * FROM site_settings WHERE id = 1").fetchone()
    out = dict(row)
    out["reviewer_self_signup"] = bool(out["reviewer_self_signup"])
    out["join_enabled"] = bool(out["join_enabled"])
    out["appearance"] = appearance.with_defaults(_loads(out.pop("appearance_json"), {}))
    out["setup_steps_done"] = _loads(out.pop("setup_steps_done_json"), [])
    return out


def _loads(raw: str, default):
    try:
        value = json.loads(raw or "")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def labels(conn: sqlite3.Connection) -> dict:
    current = settings(conn)
    return {role: current[f"role_label_{role}"] for role in accounts.ROLES}


def update(conn: sqlite3.Connection, actor, changes: dict) -> dict:
    """Applies the fields the actor may change, refusing the rest by name."""
    allowed = ADMIN_FIELDS + (OWNER_FIELDS if accounts.is_owner(actor) else ())
    refused = sorted(k for k in changes if k not in allowed and k != "appearance")
    if refused:
        raise PermissionError(f"only the owner can change {', '.join(refused)}")
    values = {}
    for key, value in changes.items():
        if key == "appearance":
            continue
        if key in ("reviewer_self_signup", "join_enabled"):
            values[key] = 1 if value else 0
            continue
        text = " ".join(str(value or "").split())
        if key in ("product_name", "role_label_owner", "role_label_admin", "role_label_reviewer") and not text:
            raise SettingsError(f"{key.replace('_', ' ')} cannot be blank")
        if len(text) > TEXT_LIMITS[key]:
            raise SettingsError(f"{key.replace('_', ' ')} is at most {TEXT_LIMITS[key]} characters")
        values[key] = text
    if "appearance" in changes:
        current = settings(conn)["appearance"]
        merged = appearance.validate({**current, **(changes["appearance"] or {})},
                                     owner=accounts.is_owner(actor), previous=current)
        values["appearance_json"] = json.dumps(merged)
    for key, value in values.items():
        conn.execute(f"UPDATE site_settings SET {key} = ? WHERE id = 1", (value,))
    return settings(conn)


def regenerate_org_id(conn: sqlite3.Connection, actor) -> str:
    if not accounts.is_owner(actor):
        raise PermissionError("only the owner can change the organisation id")
    org_id = new_org_id()
    conn.execute("UPDATE site_settings SET org_id = ? WHERE id = 1", (org_id,))
    return org_id


# What the sign-in page and the shell draw, and nothing else: no org id, contacts or custom CSS.
PUBLIC_APPEARANCE = ("tagline", "welcome", "footer", "logo_asset", "favicon_asset", "backdrop")


def public(conn: sqlite3.Connection) -> dict:
    current = settings(conn)
    look = current["appearance"]
    return {
        "product_name": current["product_name"],
        "company_name": current["company_name"],
        "labels": {role: current[f"role_label_{role}"] for role in accounts.ROLES},
        "join_enabled": current["join_enabled"],
        "self_signup": current["reviewer_self_signup"],
        "appearance": {key: look[key] for key in PUBLIC_APPEARANCE},
        "album": _album(conn, look["backdrop"]["album"]["assets"]),
    }


def _album(conn: sqlite3.Connection, asset_ids: list[int]) -> list[dict]:
    """The album's photographs in the owner's order, with their captions and credits."""
    found = {a["id"]: a for a in site_assets.listing(conn, "album_image")}
    return [{"url": found[i]["url"], "caption": found[i]["caption"], "credit": found[i]["credit"]}
            for i in asset_ids if i in found]


# ---------- first-run setup ----------


def bootstrap_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / BOOTSTRAP_FILENAME


def write_bootstrap_token(db_path: str | Path) -> str:
    """A fresh single-use token beside the database, readable by its owner only where the OS allows."""
    token = secrets.token_urlsafe(24)
    path = bootstrap_path(db_path)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def bootstrap_token_matches(db_path: str | Path, offered: str | None) -> bool:
    path = bootstrap_path(db_path)
    try:
        expected = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(expected) and secrets.compare_digest(expected, (offered or "").strip())


def remove_bootstrap_token(db_path: str | Path) -> None:
    try:
        bootstrap_path(db_path).unlink()
    except FileNotFoundError:
        pass


def setup_state(conn: sqlite3.Connection) -> dict:
    current = settings(conn)
    done = set(current["setup_steps_done"]) | ({"owner"} if accounts.owner_exists(conn) else set())
    steps = [{"id": key, "title": title, "done": key in done} for key, title in SETUP_STEPS]
    return {"steps": steps, "completed_at": current["setup_completed_at"],
            "can_finish": all(step["done"] for step in steps)}


def mark_step(conn: sqlite3.Connection, step: str, done: bool = True) -> dict:
    if step not in {key for key, _ in SETUP_STEPS} or step == "owner":
        raise SettingsError(f"there is no setup step {step!r} to mark")
    current = set(settings(conn)["setup_steps_done"])
    current = current | {step} if done else current - {step}
    conn.execute("UPDATE site_settings SET setup_steps_done_json = ? WHERE id = 1",
                 (json.dumps(sorted(current)),))
    return setup_state(conn)


def finish_setup(conn: sqlite3.Connection) -> dict:
    state = setup_state(conn)
    if not state["can_finish"]:
        left = [step["title"] for step in state["steps"] if not step["done"]]
        raise SettingsError("still to do: " + "; ".join(left))
    conn.execute("UPDATE site_settings SET setup_completed_at = COALESCE(setup_completed_at, ?) "
                 "WHERE id = 1", (_now(),))
    return setup_state(conn)


def setup_finished(conn: sqlite3.Connection) -> bool:
    return settings(conn)["setup_completed_at"] is not None


def forget_asset(conn: sqlite3.Connection, asset_id: int) -> None:
    """Every place the appearance names a file that is being deleted points at nothing instead."""
    look = settings(conn)["appearance"]
    for key in ("logo_asset", "favicon_asset"):
        if look[key] == asset_id:
            look[key] = None
    for key in ("heading_font", "body_font"):
        if look[key] == asset_id:
            look[key] = "system"
    backdrop = look["backdrop"]
    backdrop["album"]["assets"] = [a for a in backdrop["album"]["assets"] if a != asset_id]
    for part, field in (("video", "asset"), ("video", "poster"), ("animation", "asset")):
        if backdrop[part][field] == asset_id:
            backdrop[part][field] = None
    if backdrop["kind"] in ("album", "video", "animation") and not (
            backdrop["album"]["assets"] if backdrop["kind"] == "album" else backdrop[backdrop["kind"]]["asset"]):
        backdrop["kind"] = "none"
    conn.execute("UPDATE site_settings SET appearance_json = ? WHERE id = 1", (json.dumps(look),))
