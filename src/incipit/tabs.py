"""Tabs: the sections of the site that hold folders, each with its own id scheme and prompt profile.

A tab's id is what every folder references. Its slug is made once from its label and routes the
browser and the export tree, so it never changes; its label is what people see and may change. The
Team tab is not a row here: it is the site's own, fixed and last.
"""

from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timezone

from . import docid

SLUG = re.compile(r"^[a-z0-9-]{2,32}$")
# Paths the site already answers to, and words a slug must not shadow.
RESERVED = frozenset("team setup api static site login logout signup health admin hub new".split())
KINDS = ("upload",)
MAX_LABEL = 40
# What a reviewer is told about a tab: enough to draw the strip and name the folder's id scheme.
PUBLIC_FIELDS = ("id", "slug", "label", "kind", "id_scheme")


class TabError(ValueError):
    """A tab change the person can see and fix."""


class TabConflict(TabError):
    """Refused because of what the tab already holds."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(value) -> str:
    label = " ".join(str(value or "").split())
    if not label or len(label) > MAX_LABEL:
        raise TabError(f"a tab needs a name of up to {MAX_LABEL} characters")
    return label


def slugify(label: str, taken: set[str]) -> str:
    """The slug a label gets: lower-case, runs of anything else as one hyphen, suffixed past a clash."""
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32].strip("-")
    if len(base) < 2:
        if not base:
            raise TabError("a tab needs a name with a letter or digit in it")
        base = f"tab-{base}"
    if base not in taken and base not in RESERVED:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:32 - len(str(n)) - 1]}-{n}"
        if candidate not in taken and candidate not in RESERVED:
            return candidate
    raise TabError("no free slug for that name")


# ---------- changing ----------


def create(conn: sqlite3.Connection, label: str, id_scheme: str = docid.DEFAULT_SCHEME,
           prompt_profile_id: int | None = None, kind: str = "upload") -> int:
    label = _label(label)
    if kind not in KINDS:
        raise TabError(f"a tab's kind is one of {', '.join(KINDS)}")
    scheme = docid.require_scheme(id_scheme)
    _require_profile(conn, prompt_profile_id)
    taken = {row["slug"] for row in conn.execute("SELECT slug FROM tabs")}
    position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tabs").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO tabs (slug, label, kind, id_scheme, position, enabled, prompt_profile_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (slugify(label, taken), label, kind, scheme, position, prompt_profile_id, _now()))
    return cur.lastrowid


def update(conn: sqlite3.Connection, tab_id: int, changes: dict) -> dict:
    """Label, enabled, prompt profile at any time; the id scheme only while the tab has no folder."""
    current = get(conn, tab_id)
    if current is None:
        raise LookupError("no such tab")
    unknown = sorted(set(changes) - {"label", "enabled", "id_scheme", "prompt_profile_id"})
    if unknown:
        raise TabError(f"a tab has no setting {', '.join(unknown)}")
    if "label" in changes:
        conn.execute("UPDATE tabs SET label = ? WHERE id = ?", (_label(changes["label"]), tab_id))
    if "enabled" in changes:
        conn.execute("UPDATE tabs SET enabled = ? WHERE id = ?", (1 if changes["enabled"] else 0, tab_id))
    if "id_scheme" in changes:
        scheme = docid.require_scheme(changes["id_scheme"])
        if scheme != current["id_scheme"] and folder_count(conn, tab_id):
            raise TabError("this tab already has folders; its id scheme is fixed")
        conn.execute("UPDATE tabs SET id_scheme = ? WHERE id = ?", (scheme, tab_id))
    if "prompt_profile_id" in changes:
        profile_id = changes["prompt_profile_id"]
        _require_profile(conn, profile_id)
        conn.execute("UPDATE tabs SET prompt_profile_id = ? WHERE id = ?", (profile_id, tab_id))
    return get(conn, tab_id)


def reorder(conn: sqlite3.Connection, order: list) -> list[dict]:
    """The given ids first, in that order; any tab left out keeps its place after them."""
    known = [row["id"] for row in conn.execute("SELECT id FROM tabs ORDER BY position, id")]
    try:
        wanted = [int(x) for x in order]
    except (TypeError, ValueError):
        raise TabError("the order is a list of tab ids") from None
    strange = [x for x in wanted if x not in known]
    if strange:
        raise TabError(f"there is no tab {strange[0]}")
    final = list(dict.fromkeys(wanted)) + [x for x in known if x not in wanted]
    for position, tab_id in enumerate(final, start=1):
        conn.execute("UPDATE tabs SET position = ? WHERE id = ?", (position, tab_id))
    return listing(conn)


def delete(conn: sqlite3.Connection, tab_id: int) -> None:
    if get(conn, tab_id) is None:
        raise LookupError("no such tab")
    folders, batches = folder_count(conn, tab_id), batch_count(conn, tab_id)
    if folders or batches:
        what = " and ".join(part for part, n in ((f"{folders} folder(s)", folders), (f"{batches} upload(s)", batches)) if n)
        raise TabConflict(f"move or delete its {what} first")
    if conn.execute("SELECT COUNT(*) FROM tabs").fetchone()[0] <= 1:
        raise TabConflict("the site keeps at least one tab")
    conn.execute("DELETE FROM tabs WHERE id = ?", (tab_id,))


def _require_profile(conn, profile_id) -> None:
    if profile_id is None:
        return
    if conn.execute("SELECT 1 FROM prompt_profiles WHERE id = ?", (profile_id,)).fetchone() is None:
        raise LookupError("no such prompt profile")


# ---------- reading ----------


def _row(row) -> dict:
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    return out


def get(conn: sqlite3.Connection, tab_id) -> dict | None:
    """One tab in the same shape as the listing, so a change answers with what the list will show."""
    return next((t for t in listing(conn) if t["id"] == tab_id), None)


def by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM tabs WHERE slug = ?", (slug,)).fetchone()
    return _row(row) if row is not None else None


def listing(conn: sqlite3.Connection) -> list[dict]:
    """Every tab in strip order, with what it holds and the name of its profile."""
    rows = conn.execute(
        "SELECT t.*, p.name AS prompt_profile_name, "
        "(SELECT COUNT(*) FROM folders f WHERE f.tab_id = t.id) AS folder_count, "
        "(SELECT COUNT(*) FROM new_batches b WHERE b.tab_id = t.id) AS batch_count "
        "FROM tabs t LEFT JOIN prompt_profiles p ON p.id = t.prompt_profile_id "
        "ORDER BY t.position, t.id").fetchall()
    return [_row(r) for r in rows]


def for_viewer(conn: sqlite3.Connection, user) -> list[dict]:
    """An administrator's full list; a reviewer's enabled tabs with the public fields only."""
    from . import accounts

    tabs = listing(conn)
    if accounts.is_admin(user):
        return tabs
    return [{k: t[k] for k in PUBLIC_FIELDS} for t in tabs if t["enabled"]]


def folder_count(conn: sqlite3.Connection, tab_id) -> int:
    return conn.execute("SELECT COUNT(*) FROM folders WHERE tab_id = ?", (tab_id,)).fetchone()[0]


def batch_count(conn: sqlite3.Connection, tab_id) -> int:
    return conn.execute("SELECT COUNT(*) FROM new_batches WHERE tab_id = ?", (tab_id,)).fetchone()[0]


def of_folder(conn: sqlite3.Connection, folder_id) -> dict | None:
    row = conn.execute("SELECT t.* FROM tabs t JOIN folders f ON f.tab_id = t.id WHERE f.folder_id = ?",
                       (folder_id,)).fetchone()
    return _row(row) if row is not None else None


def require_open(conn: sqlite3.Connection, tab_id) -> dict:
    """The tab, if it may take work; a paused or unknown tab is refused with its name."""
    tab = get(conn, tab_id)
    if tab is None:
        raise LookupError("no such tab")
    if not tab["enabled"]:
        raise TabConflict(f"the {tab['label']} tab is paused; nothing in it can change until it is resumed")
    return tab


def require_folder_open(conn: sqlite3.Connection, folder_id) -> None:
    """A folder in a paused tab takes no change; a folder with no tab is left alone."""
    tab = of_folder(conn, folder_id)
    if tab is not None and not tab["enabled"]:
        raise TabConflict(f"the {tab['label']} tab is paused; nothing in it can change until it is resumed")
