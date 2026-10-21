"""Users, sessions, folder assignments, and the event log behind the folder changelog."""

from __future__ import annotations

import sqlite3
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import db

# scrypt parameters.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16

SESSION_TOKEN_BYTES = 32
SESSION_LIFETIME = timedelta(days=14)

ROLES = ("owner", "admin", "reviewer")
# The roles that administer the site; the owner also holds the powers only one person should.
ADMIN_ROLES = ("owner", "admin")
# The roles an account can be created with or changed to; ownership only moves by transfer.
ASSIGNABLE_ROLES = ("admin", "reviewer")

# Folder lifecycle.
FOLDER_STATUSES = ("assigned", "submitted", "returned", "approved")

# Only an admin may move a folder into these.
ADMIN_ONLY_STATUSES = ("returned", "approved")

_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 8


class AuthError(RuntimeError):
    """Signing in, signing up, or acting failed for a reason the person can see and fix."""


class PermissionDenied(RuntimeError):
    """The account is real but is not allowed to do this."""


# The kinds of document the routes name; a folder's tab is a separate thing (tabs.py).
WORKFLOWS = ("new",)


# Its own constant because a CHECK cannot be altered in place: widening it to admit a new workflow
# means rebuilding the table, and the rebuild has to create exactly this.
SCHEMA_ASSIGNMENTS = """
-- One row per folder per workflow. UNIQUE is what enforces "a folder has exactly one owner":
-- two reviewers on one folder is the situation where work silently overwrites work.
CREATE TABLE IF NOT EXISTS folder_assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id   TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    assigned_to INTEGER REFERENCES users(id),
    assigned_by INTEGER REFERENCES users(id),
    status      TEXT NOT NULL DEFAULT 'assigned',
    assigned_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (folder_id, workflow)
);
"""

SCHEMA = SCHEMA_ASSIGNMENTS + """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NOCASE so "John" and "john" cannot become two accounts, which is a confusing way to lose work.
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'reviewer')),
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    colour        TEXT,
    created_by    INTEGER REFERENCES users(id),
    last_login_at TEXT,
    -- Set by an administrative reset; the account can do nothing but change it until it does.
    must_change_password INTEGER NOT NULL DEFAULT 0
);
-- A second owner is impossible, not merely unlikely: two people cannot both hold the last word.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_owner ON users(role) WHERE role = 'owner';

-- An invitation: whoever holds the code may create an account with its role. Only the code's
-- SHA-256 is kept, so the database cannot be read for working codes.
CREATE TABLE IF NOT EXISTS join_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash  TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL CHECK (role IN ('admin', 'reviewer')),
    label      TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    max_uses   INTEGER,
    uses       INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1
);

-- Only the SHA-256 of each session token, never the token itself: whoever holds the database still
-- cannot mint a working cookie from it.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- Append-only history: who did what to a folder and when. This is the changelog the editorial team
-- asked for ("completed by John, reviewed by Paul"), and it is deliberately never updated or
-- deleted -- a folder's story is the sequence of things that happened to it, and rewriting that
-- would defeat the point of keeping it.
CREATE TABLE IF NOT EXISTS folder_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id TEXT NOT NULL,
    workflow  TEXT NOT NULL,
    actor_id  INTEGER REFERENCES users(id),
    action    TEXT NOT NULL,
    note      TEXT NOT NULL DEFAULT '',
    at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_folder_events_folder ON folder_events(folder_id, workflow, id);
CREATE INDEX IF NOT EXISTS idx_assignments_reviewer ON folder_assignments(assigned_to);
"""


# Light pill colours a reviewer can wear (key, background, ink), clear of the green, amber and teal
# that already mean accepted, flagged and reviewed.
REVIEWER_COLOURS = (
    ("sky", "#e3f0fd", "#0b5cad"),
    ("lavender", "#efe8fb", "#5b3aa8"),
    ("rose", "#fce8ef", "#a3284f"),
    ("slate", "#e9edf3", "#3d4b5f"),
    ("sand", "#f4efe4", "#6b5a2e"),
    ("plum", "#f7e6f4", "#8a2f7b"),
    ("sage", "#eaf1e4", "#46602f"),
    ("cocoa", "#f1e8e3", "#6d4633"),
)
COLOUR_KEYS = tuple(key for key, _, _ in REVIEWER_COLOURS)


def init_accounts(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
def reviewer_colours(conn: sqlite3.Connection) -> dict[int, str]:
    """{admin id: colour key}, giving any admin without one the first colour nobody holds."""
    admins = conn.execute("SELECT id, colour FROM users WHERE role IN ('owner', 'admin') "
                          "ORDER BY id").fetchall()
    taken = {row["colour"] for row in admins if row["colour"] in COLOUR_KEYS}
    held = {}
    for row in admins:
        colour = row["colour"] if row["colour"] in COLOUR_KEYS else None
        if colour is None:
            free = [key for key in COLOUR_KEYS if key not in taken]
            colour = free[0] if free else COLOUR_KEYS[row["id"] % len(COLOUR_KEYS)]
            taken.add(colour)
            conn.execute("UPDATE users SET colour = ? WHERE id = ?", (colour, row["id"]))
        held[row["id"]] = colour
    return held


def set_colour(conn: sqlite3.Connection, user_id: int, colour: str) -> None:
    """A reviewer's own colour, refused when another reviewer already wears it."""
    if colour not in COLOUR_KEYS:
        raise AuthError(f"{colour!r} is not one of the reviewer colours")
    holder = conn.execute("SELECT display_name FROM users WHERE colour = ? AND id <> ?",
                          (colour, user_id)).fetchone()
    if holder is not None:
        raise PermissionDenied(f"{holder['display_name']} already uses that colour")
    conn.execute("UPDATE users SET colour = ? WHERE id = ?", (colour, user_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- passwords ----------


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`."""
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, bytes.fromhex(hash_hex))


# ---------- accounts ----------


def is_admin(user) -> bool:
    """Owner or admin: whoever administers the site."""
    return user is not None and user["role"] in ADMIN_ROLES


def is_owner(user) -> bool:
    return user is not None and user["role"] == "owner"


def _validated(username: str, password: str) -> str:
    username = (username or "").strip()
    if not _USERNAME.match(username):
        raise AuthError("usernames are 3-32 characters, letters, digits, dot, dash or underscore")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"passwords must be at least {MIN_PASSWORD_LENGTH} characters")
    return username


def _insert_user(conn, username, password, display_name, role, created_by=None) -> int:
    try:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role, password_hash, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, (display_name or username).strip(), role, hash_password(password), _now(),
             created_by))
    except sqlite3.IntegrityError as exc:
        if "users.username" in str(exc):
            raise AuthError(f"the username {username!r} is already taken") from None
        if role == "owner" and "users.role" in str(exc):
            raise AuthError("this site already has an owner") from None
        raise
    return cur.lastrowid


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "reviewer",
    created_by: int | None = None,
) -> int:
    """Creates an admin or reviewer account and returns its id; the owner is made by create_owner."""
    username = _validated(username, password)
    if role not in ASSIGNABLE_ROLES:
        raise AuthError(f"an account cannot be created with the role {role!r}")
    return _insert_user(conn, username, password, display_name, role, created_by)


def owner_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE role = 'owner'").fetchone() is not None


def create_owner(conn: sqlite3.Connection, username: str, password: str, display_name: str = "") -> int:
    """The one owner. Checked inside a write transaction, and the unique index refuses a second."""
    username = _validated(username, password)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if owner_exists(conn):
        raise AuthError("this site already has an owner")
    return _insert_user(conn, username, password, display_name, "owner")


def owner(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE role = 'owner'").fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users(conn: sqlite3.Connection, role: str | None = None) -> list[sqlite3.Row]:
    if role:
        return conn.execute(
            "SELECT * FROM users WHERE role = ? ORDER BY display_name COLLATE NOCASE", (role,)
        ).fetchall()
    return conn.execute("SELECT * FROM users ORDER BY role, display_name COLLATE NOCASE").fetchall()


def set_password(conn: sqlite3.Connection, user_id: int, password: str,
                 must_change: bool = False) -> None:
    """Every existing session for that account is dropped; `must_change` marks an administrative reset."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"passwords must be at least {MIN_PASSWORD_LENGTH} characters")
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = ? WHERE id = ?",
                 (hash_password(password), 1 if must_change else 0, user_id))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    """Deactivating is how someone leaves the project."""
    conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
    if not active:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------- who may do what to whom (the M2 permission matrix) ----------


def _target(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = get_user(conn, user_id)
    if row is None:
        raise AuthError("no such account")
    return row


def require_may_create(actor, role: str) -> None:
    """An admin brings in reviewers; only the owner brings in admins."""
    if role not in ASSIGNABLE_ROLES:
        raise AuthError(f"unknown role {role!r}")
    if is_owner(actor) or (actor is not None and actor["role"] == "admin" and role == "reviewer"):
        return
    raise PermissionDenied("only the owner can add an administrator" if role == "admin"
                           else "only an administrator can add people")


def require_may_manage(actor, target) -> None:
    """Deactivate, reactivate or reset: the owner over anyone else, an admin over reviewers."""
    if target["id"] == actor["id"]:
        raise PermissionDenied("you cannot do that to your own account here")
    if target["role"] == "owner":
        raise PermissionDenied("nobody can change the owner's account from the site")
    if is_owner(actor) or (actor["role"] == "admin" and target["role"] == "reviewer"):
        return
    raise PermissionDenied("only the owner can manage another administrator")


def deactivate(conn: sqlite3.Connection, actor, user_id: int) -> None:
    target = _target(conn, user_id)
    require_may_manage(actor, target)
    set_active(conn, user_id, False)


def reactivate(conn: sqlite3.Connection, actor, user_id: int) -> None:
    target = _target(conn, user_id)
    require_may_manage(actor, target)
    set_active(conn, user_id, True)


def reset_password(conn: sqlite3.Connection, actor, user_id: int, temporary: str) -> None:
    """A temporary password the person must replace at their next sign-in."""
    target = _target(conn, user_id)
    require_may_manage(actor, target)
    set_password(conn, user_id, temporary, must_change=True)


def change_role(conn: sqlite3.Connection, actor, user_id: int, role: str) -> None:
    if not is_owner(actor):
        raise PermissionDenied("only the owner can change someone's role")
    if role not in ASSIGNABLE_ROLES:
        raise AuthError("a role is admin or reviewer; ownership moves by transfer")
    target = _target(conn, user_id)
    if target["role"] == "owner":
        raise PermissionDenied("transfer ownership instead of changing the owner's role")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def transfer_ownership(conn: sqlite3.Connection, actor, user_id: int) -> None:
    """The owner hands the site to another active account and becomes an admin, in one transaction."""
    if not is_owner(actor):
        raise PermissionDenied("only the owner can transfer ownership")
    target = _target(conn, user_id)
    if target["id"] == actor["id"]:
        raise AuthError("you already own this site")
    if not target["active"]:
        raise AuthError("reactivate that account before handing it the site")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    # The old owner steps down first, so the one-owner index never sees two.
    conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (actor["id"],))
    conn.execute("UPDATE users SET role = 'owner' WHERE id = ?", (user_id,))


def people(conn: sqlite3.Connection) -> list[dict]:
    """Every account, for the Team tab's People card."""
    rows = conn.execute(
        "SELECT u.*, c.display_name AS created_by_name FROM users u "
        "LEFT JOIN users c ON c.id = u.created_by "
        "ORDER BY CASE u.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, "
        "u.display_name COLLATE NOCASE").fetchall()
    return [{"id": r["id"], "username": r["username"], "display_name": r["display_name"],
             "role": r["role"], "active": bool(r["active"]), "created_at": r["created_at"],
             "last_login_at": r["last_login_at"], "must_change_password": bool(r["must_change_password"]),
             "created_by": r["created_by_name"] or ""} for r in rows]


def close_own_account(conn: sqlite3.Connection, user_id: int) -> None:
    """Someone closing their own account; the owner hands the site on first."""
    row = conn.execute("SELECT role, active FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("no such account")
    if row["role"] == "owner":
        raise PermissionDenied("transfer ownership first: the site cannot be left without an owner")
    set_active(conn, user_id, False)


# ---------- sessions ----------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login(conn: sqlite3.Connection, username: str, password: str) -> str:
    """Verifies a password and returns a new session token (the raw value, to be set as a cookie --
    it is never stored and cannot be recovered later)."""
    row = get_user_by_name(conn, (username or "").strip())
    if row is None:
        # Still spend the time hashing.
        hash_password(password or "x")
        raise AuthError("that username and password do not match")
    if not verify_password(password or "", row["password_hash"]):
        raise AuthError("that username and password do not match")
    if not row["active"]:
        raise AuthError("that account has been deactivated")
    return start_session(conn, row["id"])


def start_session(conn: sqlite3.Connection, user_id: int) -> str:
    # Clear the ones that have run out, at the one moment a new one is written.
    purge_expired_sessions(conn)
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = datetime.now(timezone.utc)
    # Every way in counts as signing in: the password, the setup wizard, and a new account.
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now.isoformat(), user_id))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_token_hash(token), user_id, now.isoformat(), (now + SESSION_LIFETIME).isoformat(), now.isoformat()),
    )
    return token


def user_for_token(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    """The signed-in user, or None.

    Sessions have a fixed expiry, so authentication is intentionally read-only. Updating
    ``last_seen_at`` on every image/API request dirtied SQLite's WAL continuously and made the
    backup watcher treat an idle review screen as changing data.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
        (_token_hash(token), _now()),
    ).fetchone()
    return row


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
    return cur.rowcount


# ---------- assignments ----------


def assign_folder(
    conn: sqlite3.Connection, folder_id: str, workflow: str, reviewer_id: int, admin_id: int,
    note: str = "",
) -> None:
    """Gives a folder to a reviewer."""
    if workflow not in WORKFLOWS:
        raise AuthError(f"unknown workflow {workflow!r}")
    reviewer = get_user(conn, reviewer_id)
    if reviewer is None:
        raise AuthError("that reviewer account does not exist")

    existing = get_assignment(conn, folder_id, workflow)
    now = _now()
    conn.execute(
        "INSERT INTO folder_assignments (folder_id, workflow, assigned_to, assigned_by, status, "
        "assigned_at, updated_at) VALUES (?, ?, ?, ?, 'assigned', ?, ?) "
        "ON CONFLICT(folder_id, workflow) DO UPDATE SET "
        "assigned_to = excluded.assigned_to, assigned_by = excluded.assigned_by, "
        "status = 'assigned', updated_at = excluded.updated_at",
        (folder_id, workflow, reviewer_id, admin_id, now, now),
    )
    action = "reassigned" if existing and existing["assigned_to"] != reviewer_id else "assigned"
    # No note rather than a manufactured one.
    record_event(conn, folder_id, workflow, admin_id, action, note)
    # Handing a folder out is taking responsibility for it, so the admin who did it holds the
    # claim.
    db.claim_folder(conn, folder_id, workflow, admin_id)


def unassign_folder(conn: sqlite3.Connection, folder_id: str, workflow: str,
                    admin_id: int, note: str = "") -> str:
    """Takes a folder back from the reviewer holding it, returning their name."""
    if workflow not in WORKFLOWS:
        raise AuthError(f"unknown workflow {workflow!r}")
    existing = get_assignment(conn, folder_id, workflow)
    if existing is None or existing["assigned_to"] is None:
        raise AuthError("that folder is not assigned to anyone")
    reviewer = get_user(conn, existing["assigned_to"])
    conn.execute("DELETE FROM folder_assignments WHERE folder_id = ? AND workflow = ?",
                 (folder_id, workflow))
    name = (reviewer["display_name"] if reviewer else "") or "a reviewer"
    # The reviewer's own verdicts stay: they did that work, and it is what the admin reads.
    record_event(conn, folder_id, workflow, admin_id, "unassigned", note or f"taken back from {name}")
    return name


def get_assignment(conn: sqlite3.Connection, folder_id: str, workflow: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM folder_assignments WHERE folder_id = ? AND workflow = ?", (folder_id, workflow)
    ).fetchone()


def folders_for_reviewer(conn: sqlite3.Connection, reviewer_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM folder_assignments WHERE assigned_to = ? ORDER BY updated_at DESC", (reviewer_id,)
    ).fetchall()


def set_folder_status(
    conn: sqlite3.Connection, folder_id: str, workflow: str, status: str, actor: sqlite3.Row,
    note: str = "",
) -> None:
    """Moves a folder through the lifecycle, refusing moves the actor is not entitled to make."""
    if status not in FOLDER_STATUSES:
        raise AuthError(f"unknown status {status!r}")
    row = get_assignment(conn, folder_id, workflow)
    if row is None:
        raise AuthError(f"{folder_id} is not assigned to anyone")

    if not is_admin(actor):
        if status in ADMIN_ONLY_STATUSES:
            raise PermissionDenied(f"only an admin can mark a folder {status}")
        if row["assigned_to"] != actor["id"]:
            raise PermissionDenied("that folder is not assigned to you")

    conn.execute(
        "UPDATE folder_assignments SET status = ?, updated_at = ? WHERE folder_id = ? AND workflow = ?",
        (status, _now(), folder_id, workflow),
    )
    record_event(conn, folder_id, workflow, actor["id"], status, note)


# ---------- the changelog ----------


# Marks an event the app recorded on its own.
SYSTEM_NOTE = "[auto] "


def is_system_note(note: str) -> bool:
    return (note or "").startswith(SYSTEM_NOTE)


def record_event(
    conn: sqlite3.Connection, folder_id: str, workflow: str, actor_id: int | None, action: str,
    note: str = "",
) -> None:
    conn.execute(
        "INSERT INTO folder_events (folder_id, workflow, actor_id, action, note, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (folder_id, workflow, actor_id, action, (note or "").strip(), _now()),
    )


def folder_history(conn: sqlite3.Connection, folder_id: str, workflow: str) -> list[dict]:
    """Everything that has happened to a folder, oldest first, with the actor's name resolved --
    this is what makes "completed by John, reviewed by Paul" readable without a second lookup."""
    rows = conn.execute(
        "SELECT e.*, u.display_name, u.role FROM folder_events e "
        "LEFT JOIN users u ON u.id = e.actor_id "
        "WHERE e.folder_id = ? AND e.workflow = ? ORDER BY e.id",
        (folder_id, workflow),
    ).fetchall()
    return [
        {
            "at": r["at"],
            "action": r["action"],
            "note": r["note"],
            "by": r["display_name"] or "(deleted account)",
            "by_role": r["role"] or "",
        }
        for r in rows
    ]


def roster(conn: sqlite3.Connection) -> list[dict]:
    """Every assignment with its reviewer, admin, status and activity -- the admin's "who is doing
    what" view, and the source of the CSV export."""
    rows = conn.execute(
        "SELECT a.*, s.display_name AS reviewer_name, s.username AS reviewer_username, "
        "       b.display_name AS assigned_by_name, "
        "       (SELECT COUNT(*) FROM folder_events e "
        "         WHERE e.folder_id = a.folder_id AND e.workflow = a.workflow) AS event_count, "
        "       (SELECT MAX(at) FROM folder_events e "
        "         WHERE e.folder_id = a.folder_id AND e.workflow = a.workflow) AS last_activity "
        "FROM folder_assignments a "
        "LEFT JOIN users s ON s.id = a.assigned_to "
        "LEFT JOIN users b ON b.id = a.assigned_by "
        "ORDER BY s.display_name COLLATE NOCASE, a.folder_id"
    ).fetchall()
    return [
        {
            "folder_id": r["folder_id"],
            "workflow": r["workflow"],
            "reviewer": r["reviewer_name"] or "(unassigned)",
            "reviewer_username": r["reviewer_username"] or "",
            "assigned_by": r["assigned_by_name"] or "",
            "status": r["status"],
            "assigned_at": r["assigned_at"],
            "last_activity": r["last_activity"] or r["updated_at"],
            "events": r["event_count"],
        }
        for r in rows
    ]


# ---------- join codes ----------

# No 0/O or 1/I/L, so a code read aloud or copied from paper survives.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _code_hash(code: str) -> str:
    return hashlib.sha256(re.sub(r"[^A-Z0-9]", "", (code or "").upper()).encode("ascii")).hexdigest()


def create_join_code(conn: sqlite3.Connection, actor, role: str, label: str = "",
                     expires_days: int | None = 14, max_uses: int | None = None) -> str:
    """A new code, returned once: only its hash is kept."""
    require_may_create(actor, role)
    if expires_days is not None and not 1 <= int(expires_days) <= 365:
        raise AuthError("a code lasts between 1 and 365 days")
    if max_uses is not None and not 1 <= int(max_uses) <= 1000:
        raise AuthError("a code can be used between 1 and 1000 times")
    code = "-".join("".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(3))
    expires = ((datetime.now(timezone.utc) + timedelta(days=int(expires_days))).isoformat()
               if expires_days is not None else None)
    conn.execute(
        "INSERT INTO join_codes (code_hash, role, label, created_by, created_at, expires_at, max_uses) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_code_hash(code), role, (label or "").strip()[:80], actor["id"], _now(), expires, max_uses))
    return code


def join_codes(conn: sqlite3.Connection, actor) -> list[dict]:
    """The codes this person may see: all of them for the owner, reviewer codes for an admin."""
    rows = conn.execute(
        "SELECT j.*, u.display_name AS created_by_name FROM join_codes j "
        "LEFT JOIN users u ON u.id = j.created_by ORDER BY j.id DESC").fetchall()
    now = _now()
    return [{"id": r["id"], "role": r["role"], "label": r["label"], "created_at": r["created_at"],
             "created_by": r["created_by_name"] or "", "expires_at": r["expires_at"],
             "max_uses": r["max_uses"], "uses": r["uses"],
             "usable": bool(r["active"]) and (r["expires_at"] is None or r["expires_at"] > now)
             and (r["max_uses"] is None or r["uses"] < r["max_uses"])}
            for r in rows if is_owner(actor) or r["role"] == "reviewer"]


def revoke_join_code(conn: sqlite3.Connection, actor, code_id: int) -> None:
    row = conn.execute("SELECT role FROM join_codes WHERE id = ?", (code_id,)).fetchone()
    if row is None:
        raise AuthError("no such join code")
    require_may_create(actor, row["role"])
    conn.execute("UPDATE join_codes SET active = 0 WHERE id = ?", (code_id,))


def redeem_join_code(conn: sqlite3.Connection, code: str) -> tuple[int, str]:
    """(code id, role) for a usable code, counting the use; refused alike for every way it is not."""
    row = conn.execute("SELECT * FROM join_codes WHERE code_hash = ?", (_code_hash(code),)).fetchone()
    now = _now()
    if (row is None or not row["active"] or (row["expires_at"] is not None and row["expires_at"] <= now)
            or (row["max_uses"] is not None and row["uses"] >= row["max_uses"])):
        raise AuthError("that join code is not valid -- ask an administrator for a new one")
    conn.execute("UPDATE join_codes SET uses = uses + 1 WHERE id = ?", (row["id"],))
    return row["id"], row["role"]
