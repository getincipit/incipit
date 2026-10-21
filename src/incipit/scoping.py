"""Filtering the shared workspace down to what one person is entitled to see."""

from __future__ import annotations


import sqlite3
from . import accounts, collab, db


# The owner reviews on the admin track and holds every admin power (accounts owns the rule).
is_admin = accounts.is_admin
is_owner = accounts.is_owner


def visible_folder_ids(conn: sqlite3.Connection, user, workflow: str = "new") -> set[str] | None:
    """The folder ids this user may see, or None meaning "no restriction" (an admin)."""
    # Fail closed: a caller that lost track of who is asking gets nothing, not everything.
    if user is None:
        return set()
    if is_admin(user):
        return None
    assigned = {
        row["folder_id"] for row in accounts.folders_for_reviewer(conn, user["id"])
        if row["workflow"] == workflow
    }
    if workflow == "new":
        # Only while nobody else holds it: the upload row is kept for the audit trail, so without
        # this an uploader would keep access to a folder reassigned away from them.
        spoken_for = _folders_assigned_away(conn, workflow, user["id"])
        assigned |= uploaded_folder_ids(conn, user) - spoken_for
    return assigned


def _folders_assigned_away(conn: sqlite3.Connection, workflow: str, user_id: int) -> set[str]:
    """Folders of this workflow that belong to somebody other than the given user."""
    rows = conn.execute(
        "SELECT folder_id FROM folder_assignments "
        "WHERE workflow = ? AND assigned_to IS NOT NULL AND assigned_to <> ?",
        (workflow, user_id),
    ).fetchall()
    return {row["folder_id"] for row in rows}


def document_folder_id(conn: sqlite3.Connection, document_id: str,
                       workflow: str) -> str | None:
    """Resolve a document's owning folder, or None when the document does not exist."""
    if workflow == "new":
        return db.folder_of_document(conn, document_id)
    return None


def may_access_document(conn: sqlite3.Connection, user, document_id: str,
                        workflow: str) -> bool:
    """Whether this viewer may touch this document, not merely see it in a list."""
    folder_id = document_folder_id(conn, document_id, workflow)
    if folder_id is None:
        return False
    visible = visible_folder_ids(conn, user, workflow)
    return visible is None or folder_id in visible


def uploaded_folder_ids(conn: sqlite3.Connection, user) -> set[str]:
    """Folders this person uploaded themselves."""
    if user is None:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT folder_id FROM new_batches WHERE uploaded_by = ? AND folder_id IS NOT NULL",
        (user["id"],),
    ).fetchall()
    return {r["folder_id"] for r in rows}


def filter_folders(conn: sqlite3.Connection, user, folders: list[dict], workflow: str = "new") -> list[dict]:
    """Narrows a folder listing to what this user may see, annotating each with its assignment."""
    visible = visible_folder_ids(conn, user, workflow)
    # One pass for every folder's counts rather than one scan per folder inside the loop below.
    progress = collab.folder_progress_map(conn, workflow)
    assignments = assignment_summaries(conn, workflow)
    out = []
    for folder in folders:
        folder_id = folder.get("folder_id", "")
        if visible is not None and folder_id not in visible:
            continue
        # Progress rides along with the assignment so a card can draw both without a second request
        # -- and so the number on screen is computed from the documents rather than from a stored
        # count that could disagree with them.
        counts = progress.get(folder_id) or collab.empty_folder_progress()
        summary = assignments.get(folder_id) or _unassigned_summary(workflow)
        out.append({
            **folder,
            **summary,
            "progress": scope_progress(counts, user, summary),
        })
    return out


def scope_progress(counts: dict, user, summary: dict) -> dict:
    """Splits a folder's counts into the viewer's own reviewing and, for an admin, the reviewer's."""
    role = "admin" if is_admin(user) else "reviewer"
    mine = counts["by_role"][role]
    shared = {k: v for k, v in counts.items() if k != "by_role"}
    if role == "admin":
        theirs = counts["by_role"]["reviewer"]
        return {**shared, "viewer_role": role, "viewer": mine, "other": theirs,
                "other_name": summary.get("assigned_to_name", "")}
    # A reviewer is sent their own counts and nothing else -- not even merged ones, which carry the
    # admin's marks inside them and would say "3 needing work" on a card before any note
    # arrived.
    return {**shared, **mine, "viewer_role": role, "viewer": mine,
            "label": (f"{mine['accepted']} of {counts['total']} accepted" if counts["total"]
                      else "no documents yet")}


def assignment_summary(conn: sqlite3.Connection, folder_id: str, workflow: str = "new") -> dict:
    """Who holds a folder, what state it is in, and the most recent note about it."""
    return assignment_summaries(conn, workflow).get(folder_id) or _unassigned_summary(workflow)


def _unassigned_summary(workflow: str) -> dict:
    return {"assigned_to": None, "assigned_to_name": "", "assigned_by": None,
            "assigned_by_name": "", "assignment_status": "unassigned", "latest_note": "",
            "latest_note_by": "", "workflow": workflow}


def assignment_summaries(conn: sqlite3.Connection, workflow: str) -> dict[str, dict]:
    """Every assignment and latest human note in two queries, independent of folder count."""
    latest_notes: dict[str, tuple[str, str]] = {}
    for row in conn.execute(
        "SELECT e.folder_id, e.note, u.display_name FROM folder_events e "
        "LEFT JOIN users u ON u.id = e.actor_id "
        "WHERE e.workflow = ? AND e.note <> '' AND e.note NOT LIKE ? ORDER BY e.id DESC",
        (workflow, accounts.SYSTEM_NOTE + "%"),
    ):
        latest_notes.setdefault(
            row["folder_id"], (row["note"], row["display_name"] or "")
        )

    out = {}
    for row in conn.execute(
        "SELECT a.*, reviewer.display_name AS assigned_to_name, "
        "assigner.display_name AS assigned_by_name FROM folder_assignments a "
        "LEFT JOIN users reviewer ON reviewer.id = a.assigned_to "
        "LEFT JOIN users assigner ON assigner.id = a.assigned_by WHERE a.workflow = ?",
        (workflow,),
    ):
        note, note_by = latest_notes.get(row["folder_id"], ("", ""))
        out[row["folder_id"]] = {
            "assigned_to": row["assigned_to"],
            "assigned_to_name": row["assigned_to_name"] or "",
            "assigned_by": row["assigned_by"],
            "assigned_by_name": row["assigned_by_name"] or "",
            "workflow": workflow,
            "assignment_status": row["status"],
            "latest_note": note,
            "latest_note_by": note_by,
        }
    return out


def filter_batches(conn: sqlite3.Connection, user, batches: list[dict]) -> list[dict]:
    """Pending uploads, narrowed and attributed."""
    admin = is_admin(user)
    batch_ids = [batch.get("batch_id") for batch in batches if batch.get("batch_id")]
    owners = {}
    if batch_ids:
        placeholders = ",".join("?" * len(batch_ids))
        owners = {
            row["batch_id"]: row for row in conn.execute(
                "SELECT b.batch_id, b.uploaded_by, u.display_name FROM new_batches b "
                "LEFT JOIN users u ON u.id = b.uploaded_by "
                f"WHERE b.batch_id IN ({placeholders})", tuple(batch_ids)
            )
        }
    out = []
    for batch in batches:
        row = owners.get(batch.get("batch_id"))
        uploader_id = row["uploaded_by"] if row else None
        if not admin and uploader_id != (user["id"] if user else None):
            continue
        out.append({
            **batch,
            "uploaded_by": uploader_id,
            "uploaded_by_name": (row["display_name"] if row and row["display_name"] else ""),
        })
    return out


def filter_documents(conn: sqlite3.Connection, user, documents: list[dict], workflow: str = "new") -> list[dict]:
    """A review queue narrowed to the folders this user holds."""
    visible = visible_folder_ids(conn, user, workflow)
    if visible is None:
        return documents

    out = []
    for document in documents:
        folder_id = document.get("folder_id") or db.folder_of_document(
            conn, document.get("document_id", ""))
        if folder_id in visible:
            out.append(document)
    return out


def visible_activity(user, entries: list[dict]) -> list[dict]:
    """The activity banner, filtered."""
    if is_admin(user):
        return entries
    if user is None:
        return []
    return [e for e in entries if e.get("owner_id") == user["id"]]


# A folder listing is assembled for the admin's card and then narrowed for anyone else.
ADMIN_ONLY_FOLDER_FIELDS = ("accepted_count", "all_approved", "export", "claim",
                            "cost_estimate", "assigned_by", "reviewers")


def strip_admin_fields(folders: list[dict], user) -> list[dict]:
    if is_admin(user):
        return folders
    return [{k: v for k, v in f.items() if k not in ADMIN_ONLY_FOLDER_FIELDS} for f in folders]
