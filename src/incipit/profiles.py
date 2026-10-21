"""Prompt profiles and their immutable versions: what a tab's drafts are told, and what every drafted
document was told.

A profile is a name and a pointer to its current version. A version is written once, from compiled
answers, and never changed: a document pins the version that drafted it, so its fields, review form
and export columns stay what they were even after the profile moves on.
"""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime, timezone

from . import prompting

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_profiles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    current_version_id INTEGER,
    created_by         INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- Written once: the triggers below refuse every later change.
CREATE TABLE IF NOT EXISTS prompt_profile_versions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id               INTEGER NOT NULL REFERENCES prompt_profiles(id),
    version_no               INTEGER NOT NULL,
    answers_json             TEXT NOT NULL,
    field_registry_json      TEXT NOT NULL,
    index_styles_json        TEXT NOT NULL,
    compiled_document_prompt TEXT NOT NULL,
    compiled_split_prompt    TEXT NOT NULL,
    compiled_merge_prompt    TEXT NOT NULL,
    document_schema_json     TEXT NOT NULL,
    merge_schema_json        TEXT NOT NULL,
    content_hash             TEXT NOT NULL,
    created_by               INTEGER,
    created_at               TEXT NOT NULL,
    UNIQUE (profile_id, version_no)
);

CREATE TRIGGER IF NOT EXISTS prompt_versions_are_never_changed
BEFORE UPDATE ON prompt_profile_versions
BEGIN SELECT RAISE(ABORT, 'a prompt profile version cannot be changed; make a new version'); END;

CREATE TRIGGER IF NOT EXISTS prompt_versions_are_never_deleted
BEFORE DELETE ON prompt_profile_versions
BEGIN SELECT RAISE(ABORT, 'a prompt profile version cannot be deleted'); END;

-- A sample draft from Settings -> Prompt. Its cost counts toward the spend cap (M5's ledger replaces this).
CREATE TABLE IF NOT EXISTS prompt_tests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    INTEGER NOT NULL REFERENCES prompt_profile_versions(id),
    created_by    INTEGER,
    created_at    TEXT NOT NULL,
    image_count   INTEGER NOT NULL,
    cost_estimate REAL,
    result_json   TEXT NOT NULL
);
"""

DEFAULT_PROFILE_NAME = "General correspondence"

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_profiles(conn: sqlite3.Connection) -> int:
    """The tables, and the default profile on first run; returns the default version's id."""
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT current_version_id FROM prompt_profiles ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        return row[0]
    profile_id = create_profile(conn, DEFAULT_PROFILE_NAME, {}, None)
    return conn.execute("SELECT current_version_id FROM prompt_profiles WHERE id = ?",
                        (profile_id,)).fetchone()[0]


def _store_version(conn, profile_id: int, compiled: prompting.Compiled, actor_id) -> int:
    number = conn.execute("SELECT COALESCE(MAX(version_no), 0) + 1 FROM prompt_profile_versions "
                          "WHERE profile_id = ?", (profile_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO prompt_profile_versions (profile_id, version_no, answers_json, field_registry_json, "
        "index_styles_json, compiled_document_prompt, compiled_split_prompt, compiled_merge_prompt, "
        "document_schema_json, merge_schema_json, content_hash, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (profile_id, number, json.dumps(compiled.answers), json.dumps(compiled.registry),
         json.dumps(compiled.styles), compiled.document_prompt, compiled.split_prompt,
         compiled.merge_prompt, json.dumps(compiled.document_schema), json.dumps(compiled.merge_schema),
         compiled.content_hash, actor_id, _now()))
    conn.execute("UPDATE prompt_profiles SET current_version_id = ?, updated_at = ? WHERE id = ?",
                 (cur.lastrowid, _now(), profile_id))
    return cur.lastrowid


def create_profile(conn, name: str, answers: dict, actor_id) -> int:
    """A new profile and its version 1; CompileError leaves nothing behind."""
    name = " ".join(str(name or "").split())
    if not name or len(name) > 80:
        raise prompting.CompileError("a profile needs a name of up to 80 characters")
    compiled = prompting.compile(answers)
    cur = conn.execute("INSERT INTO prompt_profiles (name, created_by, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?)", (name, actor_id, _now(), _now()))
    _store_version(conn, cur.lastrowid, compiled, actor_id)
    return cur.lastrowid


def add_version(conn, profile_id: int, answers: dict, actor_id) -> int:
    """A new version from edited answers, made current; the old one stays exactly as it was."""
    if profile(conn, profile_id) is None:
        raise LookupError("no such prompt profile")
    return _store_version(conn, profile_id, prompting.compile(answers), actor_id)


def set_current(conn, profile_id: int, version_id: int) -> None:
    row = conn.execute("SELECT 1 FROM prompt_profile_versions WHERE id = ? AND profile_id = ?",
                       (version_id, profile_id)).fetchone()
    if row is None:
        raise LookupError("that version is not one of this profile's")
    conn.execute("UPDATE prompt_profiles SET current_version_id = ?, updated_at = ? WHERE id = ?",
                 (version_id, _now(), profile_id))


def rename(conn, profile_id: int, name: str) -> None:
    name = " ".join(str(name or "").split())
    if not name or len(name) > 80:
        raise prompting.CompileError("a profile needs a name of up to 80 characters")
    conn.execute("UPDATE prompt_profiles SET name = ?, updated_at = ? WHERE id = ?", (name, _now(), profile_id))


# ---------- reading ----------


def profile(conn, profile_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM prompt_profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row is not None else None


def profiles(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, v.version_no AS current_version_no, "
        "(SELECT COUNT(*) FROM prompt_profile_versions x WHERE x.profile_id = p.id) AS version_count, "
        "(SELECT GROUP_CONCAT(t.label, ', ') FROM tabs t WHERE t.prompt_profile_id = p.id) AS used_by "
        "FROM prompt_profiles p LEFT JOIN prompt_profile_versions v ON v.id = p.current_version_id "
        "ORDER BY p.id").fetchall()
    return [dict(r) for r in rows]


def versions(conn, profile_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT v.id, v.version_no, v.content_hash, v.created_at, u.display_name AS created_by, "
        "(SELECT COUNT(*) FROM new_documents d WHERE d.prompt_version_id = v.id) AS documents "
        "FROM prompt_profile_versions v LEFT JOIN users u ON u.id = v.created_by "
        "WHERE v.profile_id = ? ORDER BY v.version_no DESC", (profile_id,)).fetchall()
    return [dict(r) for r in rows]


def version(conn, version_id: int) -> dict | None:
    """One version as stored, with its JSON parsed."""
    row = conn.execute(
        "SELECT v.*, p.name AS profile_name FROM prompt_profile_versions v "
        "JOIN prompt_profiles p ON p.id = v.profile_id WHERE v.id = ?", (version_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in ("answers", "field_registry", "index_styles", "document_schema", "merge_schema"):
        out[key] = json.loads(out.pop(f"{key}_json"))
    return out


def compiled(conn, version_id: int) -> prompting.Compiled:
    """What a draft under this version is told: the stored text, never recompiled."""
    v = version(conn, version_id)
    if v is None:
        raise LookupError(f"prompt version {version_id} does not exist")
    return prompting.Compiled(
        answers=v["answers"], registry=v["field_registry"], styles=v["index_styles"],
        document_prompt=v["compiled_document_prompt"], split_prompt=v["compiled_split_prompt"],
        merge_prompt=v["compiled_merge_prompt"], document_schema=v["document_schema"],
        merge_schema=v["merge_schema"], content_hash=v["content_hash"])


def label(conn, version_id) -> str:
    """"General correspondence v3", how a version is named on screen and in analytics."""
    row = conn.execute("SELECT p.name, v.version_no FROM prompt_profile_versions v JOIN prompt_profiles p "
                       "ON p.id = v.profile_id WHERE v.id = ?", (version_id,)).fetchone()
    return f"{row[0]} v{row[1]}" if row else ""


def default_version_id(conn) -> int:
    """The first profile's current version; a read only, so it is safe inside any transaction."""
    row = conn.execute("SELECT current_version_id FROM prompt_profiles ORDER BY id LIMIT 1").fetchone()
    return row[0] if row is not None else init_profiles(conn)


def version_for_tab(conn, tab_id: int) -> int:
    """The current version of the tab's profile, or of the default profile."""
    row = conn.execute("SELECT p.current_version_id FROM tabs t JOIN prompt_profiles p "
                       "ON p.id = t.prompt_profile_id WHERE t.id = ?", (tab_id,)).fetchone()
    return row[0] if row is not None and row[0] else default_version_id(conn)


def version_for_folder(conn, folder_id) -> int:
    row = conn.execute("SELECT tab_id FROM folders WHERE folder_id = ?", (folder_id,)).fetchone()
    return version_for_tab(conn, row[0] if row is not None else 1)




def document_version_id(conn, row) -> int:
    """The version a document was drafted under; an unpinned or vanished one reads as the default."""
    pinned = row["prompt_version_id"] if "prompt_version_id" in row.keys() else None
    if pinned and conn.execute("SELECT 1 FROM prompt_profile_versions WHERE id = ?", (pinned,)).fetchone():
        return pinned
    if pinned:
        logger.warning("document %s is pinned to missing prompt version %s; reading it with the default",
                       row["document_id"] if "document_id" in row.keys() else "?", pinned)
    return default_version_id(conn)


def registry_for(conn, row) -> list[dict]:
    """The registry a document's fields are read and reviewed with."""
    return compiled(conn, document_version_id(conn, row)).registry


def repin_folder(conn, folder_id: str, version_id: int) -> int:
    """Re-pins a folder's documents to another version without redrafting; values stay by key."""
    if version(conn, version_id) is None:
        raise LookupError("no such prompt version")
    return conn.execute("UPDATE new_documents SET prompt_version_id = ? WHERE folder_id = ?",
                        (version_id, folder_id)).rowcount


def record_test(conn, version_id: int, actor_id, image_count: int, cost, result: dict) -> int:
    return conn.execute(
        "INSERT INTO prompt_tests (version_id, created_by, created_at, image_count, cost_estimate, result_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (version_id, actor_id, _now(), image_count, cost, json.dumps(result))).lastrowid


def test_spend(conn) -> float:
    return conn.execute("SELECT COALESCE(SUM(cost_estimate), 0) FROM prompt_tests").fetchone()[0]


