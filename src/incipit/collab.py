"""The parts of the round trip that are about people rather than documents."""

from __future__ import annotations

from datetime import datetime, timezone

import sqlite3
from . import accounts, db

SCHEMA = """
-- One comment on one document. Threaded only by document: a flat list per document is what the
-- work actually needs ("check the date on page 3"), and nesting invites conversations that belong
-- in email.
CREATE TABLE IF NOT EXISTS document_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    author_id   INTEGER REFERENCES users(id),
    body        TEXT NOT NULL,
    -- A comment is a request for a change; resolved records that the change happened. Kept rather
    -- than deleted so the reason for an edit survives the edit.
    resolved    INTEGER NOT NULL DEFAULT 0,
    resolved_by INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);

-- Every prior value of a reviewed field, written BEFORE it is overwritten. Append-only.
--
-- Without this a reviewer's correction silently replaced the previous one and the earlier text was
-- gone: no way to see what changed, who changed it, or to undo a bad edit. Both comparable
-- platforms treat version history as core rather than a nicety, and the reason is the same here --
-- transcription is a chain of judgements, and the chain is the evidence.
CREATE TABLE IF NOT EXISTS text_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    field       TEXT NOT NULL,
    previous    TEXT,
    current     TEXT,
    -- Old installations stored only `previous`.  Keeping this marker makes a genuine removal
    -- (whose current value is NULL) distinguishable from one of those legacy rows.
    has_current INTEGER NOT NULL DEFAULT 1,
    -- The field's name when the edit was made, so a later rename does not relabel the past.
    label       TEXT,
    author_id   INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);

-- How far each person has read their own notifications. One row per user: the id of the last
-- folder_event they have seen. Cheap, and it cannot disagree with the events themselves because it
-- stores a position rather than a copy.
CREATE TABLE IF NOT EXISTS event_reads (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id),
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_document ON document_comments(document_id, workflow, id);
CREATE INDEX IF NOT EXISTS idx_revisions_document ON text_revisions(document_id, workflow, id);
"""


def init_collab(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- progress: computed, never stored ----------


ROLES = ("admin", "reviewer")

# The marker an administrator leaves on the documents they picked out when sending a folder back.
# It was once called "flagged", from the Flag button that has since been retired in favour of
# comments; the site has always shown it as "Needs work".
NEEDS_WORK = "needs_work"

# A document counts as looked at when it carries one of these.
VERDICTS = ("accepted", NEEDS_WORK)


def _empty_counts() -> dict:
    counts = {"total": 0, "drafted": 0, "reviewed": 0, "accepted": 0, "needs_work": 0}
    # Kept apart as well as merged: the merged numbers answer "how far is this folder", the per-
    # role ones answer "how far am I", and a card that shows one for the other is what made a
    # folder read as finished the moment a reviewer handed it in.
    counts["by_role"] = {role: {"reviewed": 0, "accepted": 0, "needs_work": 0} for role in ROLES}
    return counts


def _finish_counts(counts: dict) -> dict:
    counts["outstanding"] = counts["total"] - counts["reviewed"]
    counts["percent"] = round(100 * counts["reviewed"] / counts["total"]) if counts["total"] else 0
    for role, theirs in counts["by_role"].items():
        theirs["total"] = counts["total"]
        theirs["outstanding"] = counts["total"] - theirs["reviewed"]
        theirs["percent"] = (round(100 * theirs["reviewed"] / counts["total"])
                             if counts["total"] else 0)
    counts["label"] = (
        f"{counts['accepted']} of {counts['total']} accepted" if counts["total"]
        else "no documents yet"
    )
    return counts


def empty_folder_progress() -> dict:
    """A finished zero-document progress payload without rescanning either document table."""
    return _finish_counts(_empty_counts())


def _verdict(shared: str | None, per_role: dict[str, str], document_id: str) -> str:
    """One document's standing, counting whoever has actually ruled on it."""
    if shared in VERDICTS:
        return shared
    theirs = per_role.get(document_id)
    return theirs if theirs in VERDICTS else ""


def folder_progress_map(conn: sqlite3.Connection, workflow: str = "new") -> dict[str, dict]:
    """{folder_id: counts} for every folder in one pass."""
    buckets: dict[str, dict] = {}
    # One read for the whole map rather than per document.
    by_role: dict[str, dict[str, str]] = {
        role: db.get_document_reviews(conn, workflow, role) for role in ROLES}
    per_role: dict[str, str] = {}
    for role in ROLES:
        for document_id, status in by_role[role].items():
            if per_role.get(document_id) != NEEDS_WORK:
                per_role[document_id] = status

    def bucket(folder_id: str) -> dict:
        return buckets.setdefault(folder_id, _empty_counts())

    def count(counts: dict, document_id: str, shared: str | None) -> None:
        verdict = _verdict(shared, per_role, document_id)
        if verdict:
            counts[verdict] += 1
            counts["reviewed"] += 1
        for role in ROLES:
            # An admin's verdict also lives on the document's own column, which is what export and
            # the approval gate read; a reviewer's lives only in document_reviews.
            theirs = by_role[role].get(document_id)
            if role == "admin" and shared in VERDICTS:
                theirs = shared
            if theirs in VERDICTS:
                counts["by_role"][role][theirs] += 1
                counts["by_role"][role]["reviewed"] += 1

    rows = conn.execute("SELECT document_id, folder_id, review_status, status FROM new_documents")
    for r in rows:
        counts = bucket(r["folder_id"])
        counts["total"] += 1
        if r["status"] == "drafted":
            counts["drafted"] += 1
        count(counts, r["document_id"], r["review_status"])

    return {folder_id: _finish_counts(c) for folder_id, c in buckets.items()}


def folder_progress(conn: sqlite3.Connection, folder_id: str, workflow: str = "new") -> dict:
    """How far one folder actually is, counted from its documents."""
    return folder_progress_map(conn, workflow).get(folder_id) or empty_folder_progress()


class NotReady(RuntimeError):
    """A lifecycle move refused because the documents do not support it."""


def _accepted_by_admin(conn: sqlite3.Connection, folder_id: str, workflow: str) -> int:
    """Documents in a folder whose shared review_status is 'accepted'."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM new_documents WHERE folder_id = ? AND review_status = 'accepted'",
        (folder_id,)).fetchone()
    return int(row["n"])


def check_can_submit(conn: sqlite3.Connection, folder_id: str, workflow: str) -> dict:
    """A reviewer may hand back a folder only once every document has been looked at."""
    progress = folder_progress(conn, folder_id, workflow)
    if progress["total"] == 0:
        raise NotReady(
            "this folder has no drafted documents yet, so there is nothing to send for review"
        )
    if progress["outstanding"]:
        raise NotReady(
            f"{progress['outstanding']} of {progress['total']} document(s) still need reviewing "
            f"before this folder can go back — accept each one first"
        )
    sent_back = documents_sent_back(conn, folder_id, workflow)
    if sent_back:
        names = sorted(db.display_id_of(conn, document_id) or document_id for document_id in sent_back)
        raise NotReady(
            f"{len(sent_back)} document(s) were sent back for changes — open each one and "
            f"accept it before returning the folder: {', '.join(names)}"
        )
    return progress


def documents_sent_back(conn: sqlite3.Connection, folder_id: str | None,
                        workflow: str) -> set[str]:
    """Documents the admin picked out when returning a folder, that the reviewer has not looked
    at since."""
    admin = db.get_document_reviews(conn, workflow, "admin")
    reviewer = db.get_document_reviews(conn, workflow, "reviewer")
    sent_back = {
        document_id for document_id, status in admin.items()
        if status == NEEDS_WORK and reviewer.get(document_id) not in VERDICTS
    }
    # A mark counts only while its folder is actually returned: 'flagged' once also meant the
    # retired Flag button, and those verdicts were migrated to this same value.
    if sent_back and _table_exists(conn, "folder_assignments"):
        for row in conn.execute(
                "SELECT folder_id FROM folder_assignments WHERE workflow = ? AND status <> 'returned'",
                (workflow,)).fetchall():
            sent_back -= _documents_in(conn, row["folder_id"], workflow)
    if folder_id is None:
        return sent_back
    return sent_back & _documents_in(conn, folder_id, workflow)


def _documents_in(conn: sqlite3.Connection, folder_id: str, workflow: str) -> set[str]:
    rows = conn.execute("SELECT document_id FROM new_documents WHERE folder_id = ?",
                        (folder_id,)).fetchall()
    return {r["document_id"] for r in rows}


def check_can_approve(conn: sqlite3.Connection, folder_id: str, workflow: str) -> dict:
    """An admin may approve only when every document is accepted."""
    progress = folder_progress(conn, folder_id, workflow)
    if progress["total"] == 0:
        raise NotReady("this folder has no documents, so there is nothing to approve")
    accepted = _accepted_by_admin(conn, folder_id, workflow)
    if accepted != progress["total"]:
        blocked = progress["total"] - accepted
        raise NotReady(
            f"{blocked} of {progress['total']} document(s) are not accepted yet"
            + (f" ({progress['needs_work']} needing work)" if progress["needs_work"] else "")
        )
    return progress


def _document_is_in(conn: sqlite3.Connection, document_id: str, folder_id: str,
                    workflow: str) -> bool:
    """Whether a document really sits in the folder being acted on."""
    return db.folder_of_document(conn, document_id) == folder_id


def mark_documents_for_rework(conn: sqlite3.Connection, folder_id: str, workflow: str,
                              document_ids: list[str], admin_id: int | None = None) -> int:
    """Marks the documents an administrator picked out when sending a folder back."""
    marked = 0
    for document_id in document_ids:
        if not _document_is_in(conn, document_id, folder_id, workflow):
            continue
        cursor = conn.execute(
            "UPDATE new_documents SET review_status = ? WHERE document_id = ?",
            (NEEDS_WORK, document_id))
        if cursor.rowcount:
            # The administrator's own verdict, kept per role like every other one they record.
            db.set_document_review(conn, document_id, workflow, "admin", NEEDS_WORK, "")
            # And the reviewer's own accept is withdrawn, so the document returns to their queue as
            # something to do.
            db.set_document_review(conn, document_id, workflow, "reviewer", "unreviewed", "")
            marked += 1
    return marked


def _folder_holding(conn: sqlite3.Connection, document_id: str, workflow: str) -> str | None:
    """Which folder a document belongs to."""
    return db.folder_of_document(conn, document_id)


def approve_when_every_document_is_accepted(conn: sqlite3.Connection, folder_id: str,
                                            workflow: str, actor_id: int | None = None) -> bool:
    """A folder whose last document has just been accepted is approved, rather than waiting for
    somebody to press a button that could only ever say yes."""
    assignment = accounts.get_assignment(conn, folder_id, workflow)
    if assignment is None or assignment["status"] != "submitted":
        return False
    try:
        check_can_approve(conn, folder_id, workflow)
    except NotReady:
        return False
    conn.execute(
        "UPDATE folder_assignments SET status = 'approved', updated_at = ? "
        "WHERE folder_id = ? AND workflow = ?", (_now(), folder_id, workflow))
    accounts.record_event(conn, folder_id, workflow, actor_id, "approved",
                          f"{accounts.SYSTEM_NOTE}every document accepted")
    return True


def approve_ready_submissions(conn: sqlite3.Connection) -> int:
    """Approve every submitted folder whose documents were all accepted before it was submitted."""
    if not _table_exists(conn, "folder_assignments"):
        return 0
    rows = conn.execute(
        "SELECT folder_id, workflow FROM folder_assignments WHERE status = 'submitted'").fetchall()
    return sum(approve_when_every_document_is_accepted(conn, row["folder_id"], row["workflow"])
               for row in rows)


def settle_approval(conn: sqlite3.Connection, document_id: str, workflow: str,
                    actor_id: int | None = None) -> bool:
    """Both directions of the same rule, after one document's verdict changed."""
    folder_id = _folder_holding(conn, document_id, workflow)
    if folder_id is None:
        return False
    return (revoke_approval_if_incomplete(conn, document_id, workflow, actor_id)
            or approve_when_every_document_is_accepted(conn, folder_id, workflow, actor_id))


def revoke_approval_if_incomplete(conn: sqlite3.Connection, document_id: str, workflow: str,
                                  actor_id: int | None = None) -> bool:
    """Sends an approved folder back to "with reviewer" when one of its documents stops being
    accepted."""
    folder_id = _folder_holding(conn, document_id, workflow)
    if folder_id is None:
        return False

    assignment = accounts.get_assignment(conn, folder_id, workflow)
    if assignment is None or assignment["status"] != "approved":
        return False
    try:
        check_can_approve(conn, folder_id, workflow)
        return False                       # still fully accepted: the approval still holds
    except NotReady as exc:
        conn.execute(
            "UPDATE folder_assignments SET status = 'submitted', updated_at = ? "
            "WHERE folder_id = ? AND workflow = ?", (_now(), folder_id, workflow))
        accounts.record_event(conn, folder_id, workflow, actor_id, "submitted",
                              f"{accounts.SYSTEM_NOTE}approval withdrawn -- {exc}")
        return True


# ---------- comments ----------


def add_comment(
    conn: sqlite3.Connection, document_id: str, workflow: str, author_id: int, body: str
) -> int:
    body = (body or "").strip()
    if not body:
        raise ValueError("a comment needs something in it")
    if len(body) > 4000:
        raise ValueError("that comment is too long (4000 characters maximum)")
    cur = conn.execute(
        "INSERT INTO document_comments (document_id, workflow, author_id, body, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (document_id, workflow, author_id, body, _now()),
    )
    return cur.lastrowid


def comments_for(conn: sqlite3.Connection, document_id: str, workflow: str = "new") -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, u.display_name, u.role FROM document_comments c "
        "LEFT JOIN users u ON u.id = c.author_id "
        "WHERE c.document_id = ? AND c.workflow = ? ORDER BY c.id",
        (document_id, workflow),
    ).fetchall()
    return [
        {
            "id": r["id"], "body": r["body"], "resolved": bool(r["resolved"]),
            "by": r["display_name"] or "(deleted account)", "by_role": r["role"] or "",
            "at": r["created_at"],
        }
        for r in rows
    ]


def comment_counts(conn: sqlite3.Connection, workflow: str = "new") -> dict[str, dict[str, int]]:
    """How many comments each document carries, and how many are still open, in one query."""
    rows = conn.execute(
        "SELECT document_id, COUNT(*) AS total, SUM(CASE WHEN resolved THEN 0 ELSE 1 END) AS open "
        "FROM document_comments WHERE workflow = ? GROUP BY document_id",
        (workflow,),
    ).fetchall()
    return {r["document_id"]: {"total": r["total"], "open": r["open"] or 0} for r in rows}


def resolve_comment(conn: sqlite3.Connection, comment_id: int, user_id: int) -> None:
    conn.execute(
        "UPDATE document_comments SET resolved = 1, resolved_by = ? WHERE id = ?",
        (user_id, comment_id),
    )


# ---------- version history ----------


# The document's own columns; every catalogue field is tracked as FIELD_PREFIX + its registry key.
TRACKED_FIELDS = ("reviewed_text", "reviewed_title", "editors_notes")
FIELD_PREFIX = "field:"

# Not a field anybody edits: the history's record that this document is the join of several, so a
# reader meeting a history that starts mid-story knows why.
MERGE_NOTE_FIELD = "merged_from"
# The same kind of note for a redraft, which replaces the text without anybody editing it.
REDRAFT_NOTE_FIELD = "redrafted"
NOTE_FIELDS = (MERGE_NOTE_FIELD, REDRAFT_NOTE_FIELD)

FIELD_LABELS = {
    "merged_from": "Merged",
    "redrafted": "Redrafted",
    "reviewed_text": "Transcription",
    "reviewed_title": "Title",
    "editors_notes": "Editor's notes",
}


def is_tracked(field: str) -> bool:
    return field in TRACKED_FIELDS or field.startswith(FIELD_PREFIX)


def _as_list(field: str, value):
    """A list field's value as a list, whether it arrives as one or as its stored JSON."""
    if isinstance(value, str) and field.startswith(FIELD_PREFIX) and value.startswith("["):
        import json

        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return parsed if isinstance(parsed, list) else value
    return value


def comparable_revision_value(field: str, value) -> str:
    """Canonical comparison without erasing meaningful transcription whitespace."""
    value = _as_list(field, value)
    if isinstance(value, list):
        import json

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = "" if value is None else str(value)
    # Outer whitespace is not metadata.  Transcription and free-form notes retain exact spacing.
    return text if field in ("reviewed_text", "editors_notes") else text.strip()


def stored_revision_value(field: str, value) -> str | None:
    """Serialize list fields consistently while retaining NULL as a real empty value."""
    if value is None:
        return None
    if isinstance(value, list):
        import json

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


# How long one person's edits to one field keep counting as the same sitting. Autosave writes
# while the reviewer is still typing, so without this a single correction would reach the history
# as a row every few seconds -- which is the history no longer being readable.
REVISION_COALESCE_SECONDS = 5 * 60


def _seconds_since(timestamp: str | None) -> float:
    """How long ago an ISO timestamp was, or forever when it cannot be read."""
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def _open_revision(conn: sqlite3.Connection, document_id: str, workflow: str, field: str,
                   author_id: int | None):
    """This author's entry for this field, while it is still the same sitting."""
    row = conn.execute(
        "SELECT id, previous, author_id, created_at FROM text_revisions "
        "WHERE document_id = ? AND workflow = ? AND field = ? ORDER BY id DESC LIMIT 1",
        (document_id, workflow, field),
    ).fetchone()
    if row is None or row["author_id"] != author_id:
        return None
    # A merge is a new document-shaped object even when it keeps one source DocID. Revisions
    # carried from its sources are history, not an autosave sitting that may be extended or erased.
    merged_after = conn.execute(
        "SELECT 1 FROM text_revisions WHERE document_id = ? AND workflow = ? "
        f"AND field IN ({','.join('?' * len(NOTE_FIELDS))}) AND id > ? LIMIT 1",
        (document_id, workflow, *NOTE_FIELDS, row["id"]),
    ).fetchone()
    if merged_after is not None:
        return None
    return row if _seconds_since(row["created_at"]) <= REVISION_COALESCE_SECONDS else None


def record_revision(
    conn: sqlite3.Connection, document_id: str, workflow: str, field: str, previous: str | None,
    current: str | None, author_id: int | None, coalesce: bool = True, label: str | None = None,
) -> None:
    """Stores both sides of one actual content edit, extending this author's open entry.

    One entry per field per sitting rather than one per save: what a reader wants to know is that
    Paul changed a word, not the seven intermediate states autosave caught on the way there. The
    first `previous` is kept, because that is what the field said before he started.
    """
    open_entry = _open_revision(conn, document_id, workflow, field, author_id) if coalesce else None
    if open_entry is not None:
        if (comparable_revision_value(field, open_entry["previous"])
                == comparable_revision_value(field, current)):
            # Edited back to where it started, so there is no longer an edit to report.
            conn.execute("DELETE FROM text_revisions WHERE id = ?", (open_entry["id"],))
            return
        conn.execute(
            "UPDATE text_revisions SET current = ?, has_current = 1, created_at = ? WHERE id = ?",
            (stored_revision_value(field, current), _now(), open_entry["id"]),
        )
        return
    conn.execute(
        "INSERT INTO text_revisions "
        "(document_id, workflow, field, previous, current, has_current, label, author_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (document_id, workflow, field, stored_revision_value(field, previous),
         stored_revision_value(field, current), label or FIELD_LABELS.get(field), author_id, _now()),
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """A merge can run against a database that predates the collaboration tables."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def carry_threads(conn: sqlite3.Connection, document_ids, workflow: str) -> dict:
    """Lift the comments and history off documents that are about to be replaced.

    A merge deletes its sources -- including the one whose id the join keeps -- so what people
    wrote on them has to be held somewhere across that gap. Read before the delete, written back
    after, by `restore_threads`.
    """
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {"comments": [], "revisions": []}
    marks = ",".join("?" * len(ids))
    params = (*ids, workflow)
    comments = conn.execute(
        f"SELECT author_id, body, resolved, resolved_by, created_at FROM document_comments "
        f"WHERE document_id IN ({marks}) AND workflow = ? ORDER BY id", params
    ).fetchall() if _table_exists(conn, "document_comments") else []
    revisions = conn.execute(
        f"SELECT field, previous, current, has_current, label, author_id, created_at FROM text_revisions "
        f"WHERE document_id IN ({marks}) AND workflow = ? ORDER BY id", params
    ).fetchall() if _table_exists(conn, "text_revisions") else []
    return {"comments": [dict(row) for row in comments],
            "revisions": [dict(row) for row in revisions]}


def restore_threads(conn: sqlite3.Connection, document_id: str, workflow: str,
                    carried: dict) -> None:
    """Put back what `carry_threads` lifted, onto the document that replaced them."""
    comments = carried.get("comments", []) if _table_exists(conn, "document_comments") else []
    revisions = carried.get("revisions", []) if _table_exists(conn, "text_revisions") else []
    for comment in comments:
        conn.execute(
            "INSERT INTO document_comments "
            "(document_id, workflow, author_id, body, resolved, resolved_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (document_id, workflow, comment["author_id"], comment["body"],
             comment["resolved"], comment["resolved_by"], comment["created_at"]))
    for revision in revisions:
        conn.execute(
            "INSERT INTO text_revisions "
            "(document_id, workflow, field, previous, current, has_current, label, author_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (document_id, workflow, revision["field"], revision["previous"], revision["current"],
             revision["has_current"], revision.get("label"), revision["author_id"],
             revision["created_at"]))


def record_merge_note(conn: sqlite3.Connection, document_id: str, workflow: str, sources,
                      author_id: int | None) -> None:
    """Write the history's note that this document is several documents joined.

    Inserted rather than recorded through record_revision, because it must never be folded into a
    neighbouring entry: two merges of one document are two separate facts.
    """
    if not _table_exists(conn, "text_revisions"):
        return
    named = " + ".join(dict.fromkeys(sources))
    conn.execute(
        "INSERT INTO text_revisions "
        "(document_id, workflow, field, previous, current, has_current, author_id, created_at) "
        "VALUES (?, ?, ?, NULL, ?, 1, ?, ?)",
        (document_id, workflow, MERGE_NOTE_FIELD, named, author_id, _now()))


def record_redraft_note(conn: sqlite3.Connection, document_id: str, workflow: str,
                        prompt_version: str, author_id: int | None) -> None:
    """Write the history's note that the model redrafted this document, which no later edit extends."""
    if not _table_exists(conn, "text_revisions"):
        return
    conn.execute(
        "INSERT INTO text_revisions "
        "(document_id, workflow, field, previous, current, has_current, author_id, created_at) "
        "VALUES (?, ?, ?, NULL, ?, 1, ?, ?)",
        (document_id, workflow, REDRAFT_NOTE_FIELD, prompt_version, author_id, _now()))


def revisions_for(conn: sqlite3.Connection, document_id: str, workflow: str = "new") -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, u.display_name FROM text_revisions r LEFT JOIN users u ON u.id = r.author_id "
        "WHERE r.document_id = ? AND r.workflow = ? ORDER BY r.id DESC",
        (document_id, workflow),
    ).fetchall()
    return [
        {
            "id": r["id"], "field": r["field"],
            "label": r["label"] or FIELD_LABELS.get(r["field"], r["field"]),
            "previous": r["previous"], "current": r["current"],
            "has_current": bool(r["has_current"]),
            "by": r["display_name"] or "(deleted account)", "at": r["created_at"],
        }
        for r in rows
    ]


# ---------- contributor statistics ----------


def _last_touched_a_document(conn: sqlite3.Connection, user) -> str | None:
    """When this person last worked on a document, as opposed to on a folder."""
    stamps = [conn.execute(
        "SELECT MAX(created_at) FROM text_revisions WHERE author_id = ?", (user["id"],)).fetchone()[0]]
    stamps.append(conn.execute(
        "SELECT MAX(reviewed_at) FROM new_documents WHERE reviewer = ?",
        (user["display_name"],)).fetchone()[0])
    return max((s for s in stamps if s), default=None)


def _returns_to(conn: sqlite3.Connection, reviewer_id: int) -> int:
    """How many times work of this reviewer's was actually sent back for changes."""
    return conn.execute(
        "SELECT COUNT(*) FROM folder_events e "
        "JOIN folder_assignments a ON a.folder_id = e.folder_id AND a.workflow = e.workflow "
        "WHERE e.action = 'returned' AND a.assigned_to = ?", (reviewer_id,)).fetchone()[0]


def contributor_stats(conn: sqlite3.Connection) -> list[dict]:
    """Per-person throughput, for the admin's supervision view."""
    out = []
    # Each map scans its workflow once. Previously folder_progress rebuilt the full map for every
    # folder held by every reviewer, making this dashboard O(reviewers * folders * documents).
    progress_maps = {workflow: folder_progress_map(conn, workflow)
                     for workflow in accounts.WORKFLOWS}
    labels = db.folder_labels(conn)
    blank = {"folder_display_id": "", "folder_title": ""}
    for user in accounts.list_users(conn, "reviewer"):
        events = conn.execute(
            "SELECT action, folder_id, workflow, at FROM folder_events WHERE actor_id = ? ORDER BY id",
            (user["id"],),
        ).fetchall()
        submissions = [e for e in events if e["action"] == "submitted"]
        held = accounts.folders_for_reviewer(conn, user["id"])
        approved = [f for f in held if f["status"] == "approved"]

        # Turnaround: assigned -> first submission, per folder. `events` is already ordered, so
        # reuse it instead of issuing one query for every held folder.
        first_submissions = {}
        for event in submissions:
            first_submissions.setdefault((event["folder_id"], event["workflow"]), event["at"])
        turnarounds = []
        for folder in held:
            first = first_submissions.get((folder["folder_id"], folder["workflow"]))
            if first:
                try:
                    delta = datetime.fromisoformat(first) - datetime.fromisoformat(folder["assigned_at"])
                    turnarounds.append(delta.total_seconds() / 3600)
                except (ValueError, TypeError):
                    pass

        documents = sum(
            (progress_maps[folder["workflow"]].get(folder["folder_id"])
             or _finish_counts(_empty_counts()))["accepted"]
            for folder in held
        )

        out.append({
            "user_id": user["id"],
            "name": user["display_name"],
            "username": user["username"],
            "folders_held": len(held),
            "folders_approved": len(approved),
            # The counts alone answer "how many" and not "which", which is the next thing a
            # supervisor asks.
            "held": [{"folder_id": f["folder_id"], "workflow": f["workflow"],
                      "status": f["status"], **labels.get(f["folder_id"], blank)} for f in held],
            "approved": [{"folder_id": f["folder_id"], "workflow": f["workflow"],
                          **labels.get(f["folder_id"], blank)} for f in approved],
            "documents_accepted": documents,
            "submissions": len(submissions),
            # Counted from the returns themselves, not inferred from submissions minus approvals.
            "returns": _returns_to(conn, user["id"]),
            "avg_turnaround_hours": round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None,
            "last_active": max(
                (t for t in (events[-1]["at"] if events else None,
                             _last_touched_a_document(conn, user)) if t), default=None),
        })
    out.sort(key=lambda r: (-r["documents_accepted"], r["name"].lower()))
    return out


# ---------- notifications ----------


def workspace_version(conn: sqlite3.Connection, user) -> str:
    """A cheap fingerprint of everything that would change what this person sees in their queue."""
    if user is None:
        return "0"
    if accounts.is_admin(user):
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS t FROM folder_assignments"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS t "
            "FROM folder_assignments WHERE assigned_to = ?",
            (user["id"],),
        ).fetchone()
    return f"{row['n']}:{row['t']}"


def notifications_for(conn: sqlite3.Connection, user, limit: int = 20) -> dict:
    """What has happened to this person's work since they last looked."""
    if user is None:
        return {"unread": 0, "items": []}

    if accounts.is_admin(user):
        # An admin cares about work arriving for review.
        rows = conn.execute(
            "SELECT e.*, u.display_name FROM folder_events e LEFT JOIN users u ON u.id = e.actor_id "
            "WHERE e.action = 'submitted' AND (e.actor_id IS NULL OR e.actor_id != ?) "
            "ORDER BY e.id DESC LIMIT ?",
            (user["id"], limit),
        ).fetchall()
    else:
        mine = {(f["folder_id"], f["workflow"]) for f in accounts.folders_for_reviewer(conn, user["id"])}
        if not mine:
            return {"unread": 0, "items": []}
        rows = [
            r for r in conn.execute(
                "SELECT e.*, u.display_name FROM folder_events e "
                "LEFT JOIN users u ON u.id = e.actor_id "
                "WHERE (e.actor_id IS NULL OR e.actor_id != ?) ORDER BY e.id DESC LIMIT 200",
                (user["id"],),
            ).fetchall()
            if (r["folder_id"], r["workflow"]) in mine
        ][:limit]

    seen = conn.execute(
        "SELECT last_event_id FROM event_reads WHERE user_id = ?", (user["id"],)
    ).fetchone()
    watermark = seen["last_event_id"] if seen else 0

    labels = db.folder_labels(conn)
    items = [
        {
            "id": r["id"], "folder_id": r["folder_id"], "workflow": r["workflow"],
            **labels.get(r["folder_id"], {"folder_display_id": "", "folder_title": ""}),
            "action": r["action"], "note": r["note"],
            "by": r["display_name"] or "someone", "at": r["at"],
            "unread": r["id"] > watermark,
        }
        for r in rows
        # An automatic event is bookkeeping, not a message: "approval withdrawn -- 1 of 4
        # document(s) are not accepted yet" would arrive in a reviewer's bell as though the admin
        # had written it to them.
        if not accounts.is_system_note(r["note"])
    ]
    return {"unread": sum(1 for i in items if i["unread"]), "items": items}


def mark_notifications_read(conn: sqlite3.Connection, user_id: int) -> None:
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM folder_events").fetchone()["m"]
    conn.execute(
        "INSERT INTO event_reads (user_id, last_event_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_event_id = excluded.last_event_id, "
        "updated_at = excluded.updated_at",
        (user_id, newest, _now()),
    )
