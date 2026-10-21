"""SQLite schema, migrations and every query the site runs."""

from __future__ import annotations

import sqlite3
import contextlib
import json
import logging
import secrets
from collections import Counter
from datetime import datetime, timezone

from . import docid

logger = logging.getLogger(__name__)

SCHEMA = """CREATE TABLE IF NOT EXISTS folder_claims (
    folder_id  TEXT NOT NULL,
    workflow   TEXT NOT NULL,
    admin_id   INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (folder_id, workflow)
);

CREATE TABLE IF NOT EXISTS document_reviews (
    document_id TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    role        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unreviewed',
    reviewer    TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (document_id, workflow, role)
);

CREATE TABLE IF NOT EXISTS folder_exports (
    folder_id      TEXT NOT NULL,
    workflow       TEXT NOT NULL,
    exported_at    TEXT NOT NULL,
    document_count INTEGER NOT NULL DEFAULT 0,
    exported_by    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (folder_id, workflow)
);

-- A tab is one stream of work with its own folders; one is seeded and M4 lets admins add more.
-- `id` is what everything references; `slug` only routes, and `id_scheme` says how its documents'
-- display ids are numbered (see docid.SCHEMES).
CREATE TABLE IF NOT EXISTS tabs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'upload' CHECK (kind IN ('upload', 'iiif')),
    id_scheme TEXT NOT NULL DEFAULT 'letters' CHECK (id_scheme IN ('letters', 'numbers', 'filename')),
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- The profile whose current version this tab's new drafts use; NULL means the default.
    prompt_profile_id INTEGER,
    created_at TEXT NOT NULL
);

-- A folder of scans. `folder_id` is opaque and permanent; `display_id` ("E2002") is what people
-- type, unique within its tab whatever its case, and `name` is the descriptive remainder of the
-- uploaded directory's name ("E2002_Advice"), editable later; `uploaded_name` keeps the original.
CREATE TABLE IF NOT EXISTS folders (
    folder_id TEXT PRIMARY KEY,
    tab_id INTEGER NOT NULL REFERENCES tabs(id),
    display_id TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    uploaded_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (tab_id, display_id)
);

CREATE TABLE IF NOT EXISTS new_batches (
    batch_id TEXT PRIMARY KEY,
    uploaded_at TEXT NOT NULL,
    image_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploaded',
    split_cost_estimate REAL,
    -- The tab the upload was made in, known before its folder is.
    tab_id INTEGER NOT NULL DEFAULT 1 REFERENCES tabs(id),
    -- The folder this batch was scanned from; NULL until someone names it.
    folder_id TEXT REFERENCES folders(folder_id),
    -- The picked directory's name, kept for a batch uploaded before its folder was named.
    uploaded_folder_name TEXT,
    -- The batch's directory under uploads/: its batch id.
    bucket_folder TEXT,
    -- How many images (in scan order) run_propose_split has fully finished examining -- lets a
    -- retry after a failure resume from here instead of re-proposing (and re-billing) images
    -- already done. See split.run_propose_split's docstring.
    split_proposal_cursor INTEGER NOT NULL DEFAULT 0,
    -- Which account uploaded this batch. Both admins need to see that a new folder has arrived and
    -- who put it there, and a reviewer needs to see their own upload progress without seeing anyone
    -- else's. NULL for batches that predate accounts.
    uploaded_by INTEGER
);

CREATE TABLE IF NOT EXISTS new_batch_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES new_batches(batch_id),
    sequence_index INTEGER NOT NULL,
    original_filename TEXT,
    bucket_key TEXT,
    bucket_uri TEXT,
    width INTEGER,
    height INTEGER,
    proposed_new_document INTEGER NOT NULL DEFAULT 0,
    is_duplicate_scan INTEGER NOT NULL DEFAULT 0,
    assigned_document_id TEXT,
    UNIQUE(batch_id, sequence_index)
);

-- `document_id` is opaque and permanent; `display_id` ("E2002AAA") is unique within its folder and
-- is the only part that renaming or renumbering ever changes.
CREATE TABLE IF NOT EXISTS new_documents (
    document_id TEXT PRIMARY KEY,
    folder_id TEXT REFERENCES folders(folder_id),
    display_id TEXT,
    batch_id TEXT REFERENCES new_batches(batch_id),
    status TEXT NOT NULL DEFAULT 'split_confirmed',
    error_message TEXT,
    -- The title the model's fields give, cached; a reviewer's and a typed override follow.
    ai_title TEXT,
    -- Reviewer overrides: every metadata field is editable, reviewed_* overriding the model's.
    reviewed_title TEXT,
    -- A reviewer-typed title that replaces the mechanically-derived one (build_document_title) --
    -- for the cases where the "[ID], Type from Author to Recipient, Date" reconstruction reads wrong
    -- even after the metadata fields are correct. NULL means "no override, keep tracking the derived
    -- title"; non-NULL is used verbatim everywhere the title is shown or exported.
    title_override TEXT,
    text TEXT,
    uncertain_words_json TEXT,
    reviewed_text TEXT,
    -- Every catalogue field, keyed as the pinned version's registry names them (fields.py): the
    -- model's draft, and what a reviewer saved (a key present means they set it).
    fields_json TEXT NOT NULL DEFAULT '{}',
    reviewed_fields_json TEXT NOT NULL DEFAULT '{}',
    ocr_draft_text TEXT,
    output_json TEXT,
    model TEXT,
    provider TEXT,
    -- The prompt profile version that drafted it, which also says how its fields are read.
    prompt_version_id INTEGER REFERENCES prompt_profile_versions(id),
    run_date TEXT,
    cost_estimate REAL,
    duration_seconds REAL,   -- how long the draft took, for the folder analytics
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    reviewer TEXT,
    editors_notes TEXT,
    reviewed_at TEXT,
    created_at TEXT
);
-- Unique per folder, not site-wide: two folders may each hold an "X1AAA".
CREATE UNIQUE INDEX IF NOT EXISTS idx_new_documents_display ON new_documents(folder_id, display_id);

-- How a reviewer turned or mirrored a page scan: shown in the viewer and applied to exported images.
-- Mirroring happens before the clockwise rotation, so the pair covers all eight orientations.
CREATE TABLE IF NOT EXISTS page_orientations (
    workflow TEXT NOT NULL,
    document_id TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,
    rotation INTEGER NOT NULL DEFAULT 0 CHECK (rotation IN (0, 90, 180, 270)),
    mirrored INTEGER NOT NULL DEFAULT 0 CHECK (mirrored IN (0, 1)),
    set_by TEXT,
    set_at TEXT,
    PRIMARY KEY (workflow, document_id, sequence_index)
);

CREATE TABLE IF NOT EXISTS new_document_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES new_documents(document_id),
    sequence_index INTEGER NOT NULL,
    bucket_key TEXT,
    bucket_uri TEXT,
    width INTEGER,
    height INTEGER,
    UNIQUE(document_id, sequence_index)
);

-- Single-row table: the admin-adjustable pacing and cost knobs (Settings menu). A fixed id=1 row
-- rather than a key/value table since there are only a handful of values; CHECK(id = 1) makes a
-- second row a schema error instead of a silent bug.
CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_total_cost REAL NOT NULL,
    new_document_batch_size INTEGER NOT NULL DEFAULT 5,
    monthly_cap REAL          -- NULL: no monthly cap, only the all-time one
);

-- Append-only record of every deletion: who removed what, when, and why. Written only by INSERT
-- and never cascaded from the rows it describes, because it must outlive them, so nothing here
-- may be a foreign key. `cost_estimate` carries the deleted rows' own model spend and
-- get_total_cost adds this table's sum back in, so deleting documents never refunds budget.
CREATE TABLE IF NOT EXISTS deletion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reason TEXT NOT NULL,
    workflow TEXT NOT NULL,        -- the tab the target belonged to
    target_kind TEXT NOT NULL,     -- 'document' | 'new_document' | 'new_folder' | 'pending_batch'
    target_id TEXT NOT NULL,       -- document_id / folder_id (may be "" for Ungrouped) / batch_id
    summary TEXT NOT NULL DEFAULT '',  -- human-readable: title, or "N documents", for the log view
    cost_estimate REAL NOT NULL DEFAULT 0,
    details_json TEXT              -- anything else worth keeping (page counts, member ids, status)
);
"""


# The seeded first tab, used where a caller names none.
DEFAULT_TAB_ID = 1


def new_folder_id() -> str:
    """An opaque, permanent folder id: nothing about it changes when the folder is renamed."""
    return "f" + secrets.token_hex(8)


def new_document_id(conn: sqlite3.Connection | None = None) -> str:
    """An opaque, permanent document id: renaming and renumbering change only its display id.
    Given a connection, it is also unused, because scans are copied under it before its row exists."""
    while True:
        document_id = "d" + secrets.token_hex(8)
        if conn is None or conn.execute("SELECT 1 FROM new_documents WHERE document_id = ?",
                                        (document_id,)).fetchone() is None:
            return document_id


IntegrityError = sqlite3.IntegrityError
DatabaseError = sqlite3.Error


def open_connection(db_path: str) -> sqlite3.Connection:
    """A bare connection reading rows by name, for a caller that keeps and closes it."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def connect(db_path: str):
    """A connection committed when the block ends cleanly, rolled back and closed otherwise."""
    conn = open_connection(db_path)
    # SQLite parses REFERENCES clauses even when enforcement is disabled. Enable it on every
    # connection so parent/child relationships are actual invariants rather than documentation.
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL mode lets readers (e.g. the review server checking on a document) proceed without waiting
    # on a writer, and vice versa -- defense in depth on top of committing incrementally.
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Every table the site needs, in the order their foreign keys demand; safe to run on every start."""
    from . import accounts, backends, collab, jobs, ledger, profiles, site_assets, site_settings

    with connect(db_path) as conn:
        accounts.init_accounts(conn)
        profiles.init_profiles(conn)
        conn.executescript(SCHEMA)
        collab.init_collab(conn)
        site_settings.init_site(conn)
        site_assets.init_assets(conn)
        backends.init_backends(conn)
        jobs.init_jobs(conn)
        # After every table whose cost columns a database from before the ledger carries over.
        ledger.init_ledger(conn)
        # The monthly cap arrived with M5; a database made before it gains the column, unset.
        if "monthly_cap" not in {row["name"] for row in conn.execute("PRAGMA table_info(app_settings)")}:
            conn.execute("ALTER TABLE app_settings ADD COLUMN monthly_cap REAL")
        # Seed the single app_settings row on first run only; ON CONFLICT leaves an existing row,
        # and any edit already made to it, untouched on every later startup.
        conn.execute(
            "INSERT INTO app_settings (id, max_total_cost, new_document_batch_size) "
            "VALUES (1, 2.0, 5) ON CONFLICT DO NOTHING"
        )
        # The first tab, which a fresh database numbers 1 (DEFAULT_TAB_ID); never with an explicit
        # id, which would leave the identity sequence behind the rows.
        conn.execute(
            "INSERT INTO tabs (slug, label, created_at) SELECT 'documents', 'Documents', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM tabs)", (datetime.now(timezone.utc).isoformat(),))


def get_new_folder_analytics(conn: sqlite3.Connection, folder_id: str) -> dict:
    """What drafting one folder cost and how long it took."""
    members = conn.execute(
        """
        SELECT d.document_id, d.status AS transcription_status, d.cost_estimate, d.duration_seconds,
               d.model, d.run_date, d.batch_id,
               pp.name || ' v' || v.version_no AS prompt_version,
               (SELECT COUNT(*) FROM new_document_pages p
                 WHERE p.document_id = d.document_id) AS page_count
        FROM new_documents d
        LEFT JOIN prompt_profile_versions v ON v.id = d.prompt_version_id
        LEFT JOIN prompt_profiles pp ON pp.id = v.profile_id
        WHERE d.folder_id = ?
        """,
        (folder_id,),
    ).fetchall()
    summary = _summarise_drafting(members, ok_status="drafted")

    batch_ids = {r["batch_id"] for r in members if r["batch_id"]}
    if batch_ids:
        placeholders = ",".join("?" * len(batch_ids))
        row = conn.execute(
            f"SELECT COALESCE(SUM(split_cost_estimate), 0) AS c FROM new_batches "
            f"WHERE batch_id IN ({placeholders})",
            tuple(batch_ids),
        ).fetchone()
        summary["split_cost"] = row["c"] or 0.0
    return summary


def _summarise_drafting(rows: list, ok_status: str = "ok") -> dict:
    """Totals and rates over one folder's documents."""
    drafted = [r for r in rows if r["transcription_status"] == ok_status]
    timed = [r for r in drafted if (r["duration_seconds"] or 0) > 0]
    seconds = sum(r["duration_seconds"] or 0.0 for r in timed)
    pages = sum(r["page_count"] or 0 for r in timed)
    run_dates = sorted(r["run_date"] for r in drafted if r["run_date"])

    return {
        "document_count": len(rows),
        "drafted_count": len(drafted),
        "page_count": sum(r["page_count"] or 0 for r in drafted),
        "cost": sum(r["cost_estimate"] or 0.0 for r in drafted),
        "split_cost": 0.0,
        "seconds": seconds,
        "measured_count": len(timed),
        "unmeasured_count": len(drafted) - len(timed),
        "seconds_per_document": seconds / len(timed) if timed else None,
        "seconds_per_page": seconds / pages if pages else None,
        "documents_per_minute": (len(timed) / seconds * 60) if seconds else None,
        "cost_per_document": (
            sum(r["cost_estimate"] or 0.0 for r in drafted) / len(drafted) if drafted else None
        ),
        "models": sorted({r["model"] for r in drafted if r["model"]}),
        # Which model drafted how many: a document the primary model refused is drafted by the
        # fallback, and this is where that shows.
        "documents_by_model": dict(sorted(Counter(
            r["model"] for r in drafted if r["model"]).items())),
        "prompt_versions": sorted({r["prompt_version"] for r in drafted if r["prompt_version"]}),
        "first_run": run_dates[0] if run_dates else None,
        "last_run": run_dates[-1] if run_dates else None,
    }


def get_total_cost(conn: sqlite3.Connection) -> float:
    """Cumulative model spend, from the ledger: every billed call, kept even when its document goes."""
    from . import ledger
    return ledger.spent(conn)














def record_deletion(
    conn: sqlite3.Connection,
    *,
    reviewer: str,
    reason: str,
    workflow: str,
    target_kind: str,
    target_id: str,
    summary: str = "",
    cost_estimate: float = 0.0,
    details: dict | None = None,
) -> None:
    """Append one row to deletion_log."""
    conn.execute(
        "INSERT INTO deletion_log (deleted_at, reviewer, reason, workflow, target_kind, target_id, "
        "summary, cost_estimate, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            reviewer,
            reason,
            workflow,
            target_kind,
            target_id,
            summary,
            cost_estimate or 0.0,
            json.dumps(details, ensure_ascii=False) if details else None,
        ),
    )


def get_new_document_recorded_cost(conn: sqlite3.Connection, document_id: str) -> float:
    """new_documents counterpart to get_document_recorded_cost -- one row per document here, so a
    plain read rather than a SUM."""
    row = conn.execute(
        "SELECT COALESCE(cost_estimate, 0) AS c FROM new_documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return (row["c"] if row else 0.0) or 0.0


def get_deletion_log(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Most recent deletions first -- what the review UI's "Deletion Log" tab lists."""
    return conn.execute(
        "SELECT id, deleted_at, reviewer, reason, workflow, target_kind, target_id, summary, "
        "cost_estimate, details_json FROM deletion_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_new_document_error(conn: sqlite3.Connection, document_ids: list[str]) -> str | None:
    """Same idea as get_transcription_error, for the "Add New Document" workflow's own per-document
    draft errors (new_documents.status = 'error')."""
    if not document_ids:
        return None
    placeholders = ",".join("?" * len(document_ids))
    row = conn.execute(
        f"SELECT error_message FROM new_documents WHERE status = 'error' AND error_message IS NOT NULL "
        f"AND document_id IN ({placeholders}) LIMIT 1",
        document_ids,
    ).fetchone()
    return row["error_message"] if row else None


def get_app_settings(conn: sqlite3.Connection) -> dict:
    """The admin-adjustable knobs: how many documents one Draft click processes, and the all-time
    model spend cap."""
    row = conn.execute("SELECT max_total_cost, new_document_batch_size, monthly_cap FROM app_settings WHERE id = 1").fetchone()
    return dict(row)


def update_app_settings(conn: sqlite3.Connection, max_total_cost: float, new_document_batch_size: int) -> None:
    conn.execute(
        "UPDATE app_settings SET max_total_cost = ?, new_document_batch_size = ? WHERE id = 1",
        (max_total_cost, new_document_batch_size),
    )


# ---------- folders ----------


def get_folder(conn: sqlite3.Connection, folder_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM folders WHERE folder_id = ?", (folder_id,)).fetchone()


def find_folder(conn: sqlite3.Connection, display_id: str,
                tab_id: int = DEFAULT_TAB_ID) -> sqlite3.Row | None:
    """The folder a tab holds under this display id, matched whatever its case."""
    return conn.execute("SELECT * FROM folders WHERE tab_id = ? AND display_id = ?",
                        (tab_id, display_id)).fetchone()


def folder_spelled_otherwise(conn: sqlite3.Connection, display_id: str,
                             tab_id: int = DEFAULT_TAB_ID) -> str | None:
    """The display id a tab already holds for this folder in another case ("E2003" for "e2003")."""
    row = find_folder(conn, display_id, tab_id)
    return row["display_id"] if row is not None and row["display_id"] != display_id else None



def ensure_folder(conn: sqlite3.Connection, display_id: str, name: str = "",
                  tab_id: int = DEFAULT_TAB_ID) -> str:
    """The folder_id for a display id in a tab, creating the folder the first time it is named."""
    docid.require_safe_id(display_id, "Folder ID")
    row = find_folder(conn, display_id, tab_id)
    if row is not None:
        if name and not row["name"]:
            conn.execute("UPDATE folders SET name = ?, uploaded_name = CASE WHEN uploaded_name = '' "
                         "THEN ? ELSE uploaded_name END WHERE folder_id = ?",
                         (name, name, row["folder_id"]))
        return row["folder_id"]
    folder_id = new_folder_id()
    conn.execute(
        "INSERT INTO folders (folder_id, tab_id, display_id, name, uploaded_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (folder_id, tab_id, display_id, name or "", name or "",
         datetime.now(timezone.utc).isoformat()))
    return folder_id


def folder_display(conn: sqlite3.Connection, folder_id: str | None) -> tuple[str, str]:
    """(display id, name) for a folder, or ("", "") when there is none."""
    row = get_folder(conn, folder_id) if folder_id else None
    return (row["display_id"], row["name"]) if row is not None else ("", "")


def folder_labels(conn: sqlite3.Connection) -> dict[str, dict]:
    """{folder_id: {"folder_display_id", "folder_title"}} for every folder, in one read -- what a
    payload naming folders by internal id adds so people see their names."""
    return {row["folder_id"]: {"folder_display_id": row["display_id"],
                               "folder_title": docid.folder_directory(row["display_id"], row["name"])
                                               or row["display_id"],
                               "tab_id": row["tab_id"], "tab_slug": row["tab_slug"] or "",
                               "tab_label": row["tab_label"] or ""}
            for row in conn.execute("SELECT f.folder_id, f.display_id, f.name, f.tab_id, t.slug AS tab_slug, "
                                    "t.label AS tab_label FROM folders f LEFT JOIN tabs t ON t.id = f.tab_id")}


def with_folder_labels(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Each item, plus its folder's display id and title (blank for a folder that is gone)."""
    labels = folder_labels(conn)
    blank = {"folder_display_id": "", "folder_title": ""}
    return [{**item, **labels.get(item.get("folder_id") or "", blank)} for item in items]


def folder_title(conn: sqlite3.Connection, folder_id: str | None) -> str:
    """What the folder is called on screen and in exports: `FolderID_Group`."""
    display_id, name = folder_display(conn, folder_id)
    return docid.folder_directory(display_id, name) or display_id




def folder_id_scheme(conn: sqlite3.Connection, folder_id: str) -> str:
    """The display-id scheme of the tab a folder belongs to."""
    row = conn.execute("SELECT t.id_scheme FROM folders f JOIN tabs t ON t.id = f.tab_id "
                       "WHERE f.folder_id = ?", (folder_id,)).fetchone()
    return row["id_scheme"] if row is not None else docid.DEFAULT_SCHEME


def folder_of_document(conn: sqlite3.Connection, document_id: str) -> str | None:
    row = conn.execute("SELECT folder_id FROM new_documents WHERE document_id = ?",
                       (document_id,)).fetchone()
    return row["folder_id"] if row is not None else None


def display_id_of(conn: sqlite3.Connection, document_id: str) -> str:
    row = conn.execute("SELECT display_id FROM new_documents WHERE document_id = ?",
                       (document_id,)).fetchone()
    return (row["display_id"] or "") if row is not None else ""


def document_by_display_id(conn: sqlite3.Connection, folder_id: str,
                           display_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM new_documents WHERE folder_id = ? AND display_id = ?",
                        (folder_id, display_id)).fetchone()


def display_ids_in_folder(conn: sqlite3.Connection, folder_id: str) -> set[str]:
    """Every display id a folder's documents use, which next_document_ids must not repeat."""
    return {row["display_id"] for row in conn.execute(
        "SELECT display_id FROM new_documents WHERE folder_id = ?", (folder_id,))}

# --------------------------------------------------------------------------- "Add New Document"
# workflow -- documents not yet on the live site at all.


def create_batch(
    conn: sqlite3.Connection, batch_id: str, image_count: int, folder_id: str | None = None,
    uploaded_folder_name: str = "", uploaded_by: int | None = None, bucket_folder: str = "",
    uploaded_at: str = "", tab_id: int = DEFAULT_TAB_ID,
) -> None:
    """`folder_id` is the folder's internal id (see ensure_folder), or None until it is named."""
    conn.execute(
        "INSERT INTO new_batches (batch_id, uploaded_at, image_count, status, folder_id, "
        "uploaded_folder_name, uploaded_by, bucket_folder, tab_id) "
        "VALUES (?, ?, ?, 'uploaded', ?, ?, ?, ?, ?)",
        (batch_id, uploaded_at or datetime.now(timezone.utc).isoformat(), image_count,
         folder_id or None, uploaded_folder_name or None, uploaded_by, bucket_folder or batch_id, tab_id),
    )


def add_batch_images(conn: sqlite3.Connection, batch_id: str, images: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO new_batch_images (
            batch_id, sequence_index, original_filename, bucket_key, bucket_uri, width, height
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                batch_id,
                img["sequence_index"],
                img.get("original_filename"),
                img.get("bucket_key"),
                img.get("bucket_uri"),
                img.get("width"),
                img.get("height"),
            )
            for img in images
        ],
    )


def get_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM new_batches WHERE batch_id = ?", (batch_id,)).fetchone()


def set_batch_folder_id(conn: sqlite3.Connection, batch_id: str, folder_id: str | None) -> None:
    """Sets (or corrects) which folder a batch belongs to, before its split is confirmed."""
    conn.execute("UPDATE new_batches SET folder_id = ? WHERE batch_id = ?",
                 (folder_id or None, batch_id))


def set_new_folder_name(conn: sqlite3.Connection, folder_id: str, folder_name: str) -> None:
    """Renames a folder; its display id, and everything keyed by its internal id, stay."""
    conn.execute("UPDATE folders SET name = ? WHERE folder_id = ?", (folder_name or "", folder_id))


def new_folder_names(conn: sqlite3.Connection, folder_id: str) -> tuple[str, str]:
    """(the folder's name now, the name it was uploaded under) -- the second is what Use Default
    Title returns to."""
    row = get_folder(conn, folder_id)
    if row is None:
        return "", ""
    return row["name"] or "", row["uploaded_name"] or ""


def get_pending_batches(conn: sqlite3.Connection, tab_id: int | None = None) -> list[sqlite3.Row]:
    """Batches that have been uploaded (and possibly split-proposed) but never confirmed -- e.g.
    the reviewer's browser was never open for the upload, or they navigated away before
    confirming."""
    rows = conn.execute(
        "SELECT * FROM new_batches WHERE status IN "
        f"({', '.join('?' * len(PENDING_BATCH_STATUSES))}) AND (? IS NULL OR tab_id = ?) "
        "ORDER BY uploaded_at DESC",
        (*PENDING_BATCH_STATUSES, tab_id, tab_id),
    ).fetchall()
    finished = {
        r["batch_id"] for r in conn.execute(
            "SELECT DISTINCT batch_id FROM new_documents WHERE batch_id IS NOT NULL")
    }
    return [r for r in rows if r["batch_id"] not in finished]


def get_batch_images(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM new_batch_images WHERE batch_id = ? ORDER BY sequence_index",
        (batch_id,),
    ).fetchall()


def get_batch_image(conn: sqlite3.Connection, batch_id: str, sequence_index: int) -> sqlite3.Row | None:
    """One page of a batch."""
    return conn.execute(
        "SELECT * FROM new_batch_images WHERE batch_id = ? AND sequence_index = ?",
        (batch_id, sequence_index),
    ).fetchone()


def delete_batch_rows(conn: sqlite3.Connection, batch_id: str) -> None:
    """Removes a batch's `new_batch_images` and `new_batches` rows -- pure DB cleanup, no bucket
    I/O (see split.delete_batch for that)."""
    conn.execute("DELETE FROM new_batch_images WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM new_batches WHERE batch_id = ?", (batch_id,))


def delete_batches_in_folder(conn: sqlite3.Connection, folder_id: str) -> int:
    """Delete a folder's intake batches in foreign-key-safe child-first order."""
    batch_ids = [
        row["batch_id"]
        for row in conn.execute(
            "SELECT batch_id FROM new_batches WHERE folder_id = ?", (folder_id,)
        )
    ]
    for batch_id in batch_ids:
        delete_batch_rows(conn, batch_id)
    return len(batch_ids)


def set_batch_split_proposal(
    conn: sqlite3.Connection,
    batch_id: str,
    proposals: dict[int, bool],
    cost_estimate: float | None = None,
    duplicates: dict[int, bool] | None = None,
    proposal_cursor: int | None = None,
) -> None:
    """`proposals` maps sequence_index -> whether that page starts a new document, per Qwen's split
    proposal."""
    conn.executemany(
        "UPDATE new_batch_images SET proposed_new_document = ? WHERE batch_id = ? AND sequence_index = ?",
        [(1 if starts_new else 0, batch_id, seq) for seq, starts_new in proposals.items()],
    )
    if duplicates:
        conn.executemany(
            "UPDATE new_batch_images SET is_duplicate_scan = ? WHERE batch_id = ? AND sequence_index = ?",
            [(1 if is_dup else 0, batch_id, seq) for seq, is_dup in duplicates.items()],
        )
    # Only while the batch is still awaiting confirmation.
    conn.execute(
        "UPDATE new_batches SET status = 'split_proposed', split_cost_estimate = ?, "
        "split_proposal_cursor = COALESCE(?, split_proposal_cursor) "
        f"WHERE batch_id = ? AND status IN ({', '.join('?' * len(PENDING_BATCH_STATUSES))})",
        (cost_estimate, proposal_cursor, batch_id, *PENDING_BATCH_STATUSES),
    )


def assign_batch_images(conn: sqlite3.Connection, batch_id: str, assignments: dict[int, str]) -> None:
    """Records which finalized document each batch image ended up in -- an audit trail back from
    the raw intake batch to the document it became, once a reviewer has confirmed the split."""
    conn.executemany(
        "UPDATE new_batch_images SET assigned_document_id = ? WHERE batch_id = ? AND sequence_index = ?",
        [(document_id, batch_id, seq) for seq, document_id in assignments.items()],
    )


# The statuses in which a batch is still waiting for a reviewer to confirm its split -- i.e. the
# ones get_pending_batches lists.
PENDING_BATCH_STATUSES = ("uploaded", "split_proposed")


def claim_folder(conn: sqlite3.Connection, folder_id: str, workflow: str, admin_id: int) -> None:
    """Records that an admin is working on a folder."""
    conn.execute(
        """
        INSERT INTO folder_claims (folder_id, workflow, admin_id, claimed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(folder_id, workflow) DO UPDATE SET
            admin_id = excluded.admin_id, claimed_at = excluded.claimed_at
        """,
        (folder_id, workflow, admin_id, datetime.now(timezone.utc).isoformat()),
    )


def release_folder(conn: sqlite3.Connection, folder_id: str, workflow: str) -> None:
    conn.execute(
        "DELETE FROM folder_claims WHERE folder_id = ? AND workflow = ?", (folder_id, workflow)
    )


def get_folder_claims(conn: sqlite3.Connection, workflow: str | None = None) -> dict[str, dict]:
    """{folder_id: {admin_id, by, claimed_at}}."""
    sql = ("SELECT c.*, u.display_name FROM folder_claims c "
           "LEFT JOIN users u ON u.id = c.admin_id")
    params: tuple = ()
    if workflow is not None:
        sql += " WHERE c.workflow = ?"
        params = (workflow,)
    return {
        r["folder_id"]: {
            "admin_id": r["admin_id"],
            "by": r["display_name"] or "(deleted account)",
            "claimed_at": r["claimed_at"],
            "workflow": r["workflow"],
        }
        for r in conn.execute(sql, params)
    }


def set_document_review(conn: sqlite3.Connection, document_id: str, workflow: str, role: str,
                        status: str, reviewer: str = "") -> None:
    """One person's verdict on one document."""
    conn.execute(
        """
        INSERT INTO document_reviews (document_id, workflow, role, status, reviewer, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id, workflow, role) DO UPDATE SET
            status = excluded.status, reviewer = excluded.reviewer,
            reviewed_at = excluded.reviewed_at
        """,
        (document_id, workflow, role, status, reviewer, datetime.now(timezone.utc).isoformat()),
    )


def get_document_reviews(conn: sqlite3.Connection, workflow: str, role: str) -> dict[str, str]:
    """{document_id: status} for one role in one workflow."""
    return {
        r["document_id"]: r["status"]
        for r in conn.execute(
            "SELECT document_id, status FROM document_reviews WHERE workflow = ? AND role = ?",
            (workflow, role),
        )
    }


def reviewed_by_folder(conn: sqlite3.Connection, workflow: str) -> dict[str, dict[str, int]]:
    """{folder_id: {reviewer name: documents they marked reviewed}}, in one read."""
    tally: dict[str, dict[str, int]] = {}
    for row in conn.execute(
            "SELECT folder_id, reviewer FROM new_documents WHERE review_status = 'accepted'"):
        names = tally.setdefault(row["folder_id"], {})
        # A review with no recorded name still counts, under "" -- the folder's total must be true.
        name = (row["reviewer"] or "").strip()
        names[name] = names.get(name, 0) + 1
    return tally


def get_role_verdicts(conn: sqlite3.Connection, workflow: str, role: str) -> dict[str, dict]:
    """{document_id: {"status", "reviewer"}} for one role."""
    return {
        r["document_id"]: {"status": r["status"], "reviewer": r["reviewer"] or ""}
        for r in conn.execute(
            "SELECT document_id, status, reviewer FROM document_reviews "
            "WHERE workflow = ? AND role = ?", (workflow, role))
    }


def get_document_review(conn: sqlite3.Connection, document_id: str, workflow: str,
                        role: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM document_reviews WHERE document_id = ? AND workflow = ? AND role = ?",
        (document_id, workflow, role),
    ).fetchone()
    return row["status"] if row else None


def record_folder_export(conn: sqlite3.Connection, folder_id: str, workflow: str,
                         document_count: int, exported_by: str = "") -> None:
    """Notes that a folder was exported, so the queue can show which ones are still outstanding."""
    conn.execute(
        """
        INSERT INTO folder_exports (folder_id, workflow, exported_at, document_count, exported_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(folder_id, workflow) DO UPDATE SET
            exported_at = excluded.exported_at,
            document_count = excluded.document_count,
            exported_by = excluded.exported_by
        """,
        (folder_id, workflow, datetime.now(timezone.utc).isoformat(), document_count, exported_by),
    )


def get_folder_exports(conn: sqlite3.Connection, workflow: str) -> dict[str, dict]:
    """{folder_id: {exported_at, document_count, exported_by}} for one workflow."""
    return {
        r["folder_id"]: {
            "exported_at": r["exported_at"],
            "document_count": r["document_count"],
            "exported_by": r["exported_by"] or "",
        }
        for r in conn.execute(
            "SELECT * FROM folder_exports WHERE workflow = ?", (workflow,)
        )
    }


def mark_batch_confirmed(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute("UPDATE new_batches SET status = 'confirmed' WHERE batch_id = ?", (batch_id,))


def batch_is_pending(conn: sqlite3.Connection, batch_id: str) -> bool:
    """Whether this batch is still awaiting confirmation."""
    row = conn.execute("SELECT status FROM new_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return row is not None and row["status"] in PENDING_BATCH_STATUSES


def get_batch_images_for_documents(conn: sqlite3.Connection, document_ids: list[str]) -> list[sqlite3.Row]:
    """The original intake images that became these documents, in the scan order they arrived in."""
    if not document_ids:
        return []
    placeholders = ",".join("?" * len(document_ids))
    return conn.execute(
        f"SELECT * FROM new_batch_images WHERE assigned_document_id IN ({placeholders}) "
        "ORDER BY batch_id, sequence_index",
        document_ids,
    ).fetchall()






def create_new_document(
    conn: sqlite3.Connection, folder_id: str, display_id: str, batch_id: str | None,
    pages: list[dict], document_id: str | None = None,
) -> str:
    """Creates one confirmed document (status='split_confirmed', ready for drafting) and its pages,
    returning its internal id. The display id must be new within the folder."""
    docid.require_safe_id(display_id, "document id")
    if document_by_display_id(conn, folder_id, display_id) is not None:
        raise ValueError(f"document id {display_id!r} is already in use in this folder")
    document_id = document_id or new_document_id()
    conn.execute(
        """
        INSERT INTO new_documents (document_id, folder_id, display_id, batch_id, status, created_at)
        VALUES (?, ?, ?, ?, 'split_confirmed', ?)
        """,
        (document_id, folder_id, display_id, batch_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.executemany(
        """
        INSERT INTO new_document_pages (document_id, sequence_index, bucket_key, bucket_uri, width, height)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                p["sequence_index"],
                p.get("bucket_key"),
                p.get("bucket_uri"),
                p.get("width"),
                p.get("height"),
            )
            for p in pages
        ],
    )
    return document_id


def rename_new_document(conn: sqlite3.Connection, document_id: str, new_display_id: str) -> None:
    """Gives a document a new display id. Nothing else changes: every other row and every stored
    file is keyed by its internal id."""
    docid.require_safe_id(new_display_id, "document id")
    row = get_new_document(conn, document_id)
    if row is None:
        raise ValueError(f"cannot rename missing document {document_id!r}")
    old_display_id = row["display_id"] or ""
    if new_display_id == old_display_id:
        return
    other = document_by_display_id(conn, row["folder_id"], new_display_id)
    if other is not None and other["document_id"] != document_id:
        raise ValueError(f"document id {new_display_id!r} is already in use in this folder")
    conn.execute("UPDATE new_documents SET display_id = ? WHERE document_id = ?",
                 (new_display_id, document_id))
    _retitle_renamed_document(conn, document_id, old_display_id, new_display_id)


def _retitle_renamed_document(conn: sqlite3.Connection, document_id: str, old_display_id: str,
                              new_display_id: str) -> None:
    """Moves the display id a title opens with onto the new one.

    Every derived title is written `[E2002AAA], Letter from ...`, so a renumbered document would
    otherwise keep announcing the id it used to have. Only that leading id is touched: the rest of
    a title may be a reviewer's own words.
    """
    if not old_display_id:
        return
    row = conn.execute(
        "SELECT ai_title, reviewed_title, title_override FROM new_documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        return
    for column in ("ai_title", "reviewed_title", "title_override"):
        title = row[column]
        if not title:
            continue
        for opening in (f"[{old_display_id}]", old_display_id):
            if title.startswith(opening):
                replacement = (f"[{new_display_id}]" if opening.startswith("[") else new_display_id)
                conn.execute(
                    f"UPDATE new_documents SET {column} = ? WHERE document_id = ?",
                    (replacement + title[len(opening):], document_id),
                )
                break


def renumber_new_documents(conn: sqlite3.Connection, plan: dict[str, str]) -> None:
    """Applies a `{document_id: new_display_id}` renumbering at once (see docid.renumber_plan).

    Two passes, through temporary display ids, so a swap never trips the per-folder uniqueness.
    """
    if not plan:
        return
    for document_id, new_display_id in plan.items():
        docid.require_safe_id(new_display_id, "document id")
        row = get_new_document(conn, document_id)
        if row is None:
            raise ValueError(f"cannot renumber missing document {document_id!r}")
        other = document_by_display_id(conn, row["folder_id"], new_display_id)
        if other is not None and other["document_id"] not in plan:
            raise ValueError(f"cannot renumber to {new_display_id!r}: that id is already in use")
    old = {document_id: display_id_of(conn, document_id) for document_id in plan}
    for index, document_id in enumerate(plan):
        conn.execute("UPDATE new_documents SET display_id = ? WHERE document_id = ?",
                     (f"__renumber_{index}__", document_id))
    for document_id, new_display_id in plan.items():
        conn.execute("UPDATE new_documents SET display_id = ? WHERE document_id = ?",
                     (new_display_id, document_id))
        _retitle_renamed_document(conn, document_id, old[document_id], new_display_id)


def get_new_document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM new_documents WHERE document_id = ?", (document_id,)).fetchone()


def purge_folder_state(conn: sqlite3.Connection, folder_id: str, workflow: str,
                       document_ids: list[str]) -> dict[str, int]:
    """Everything a folder accumulated while people worked on it, removed with the folder."""
    removed: dict[str, int] = {}

    def run(table: str, where: str, params: tuple) -> None:
        count = conn.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount
        if count:
            removed[table] = count

    run("folder_assignments", "folder_id = ? AND workflow = ?", (folder_id, workflow))
    run("folder_events", "folder_id = ? AND workflow = ?", (folder_id, workflow))
    run("folder_claims", "folder_id = ? AND workflow = ?", (folder_id, workflow))
    run("folder_exports", "folder_id = ? AND workflow = ?", (folder_id, workflow))
    for document_id in document_ids:
        run("document_reviews", "document_id = ? AND workflow = ?", (document_id, workflow))
        run("document_comments", "document_id = ? AND workflow = ?", (document_id, workflow))
        run("text_revisions", "document_id = ? AND workflow = ?", (document_id, workflow))
    return removed


def folder_has_work(conn: sqlite3.Connection, folder_id: str, workflow: str = "new") -> dict:
    """What a folder already holds, for the guardrail on re-uploading one that exists."""
    document_ids = get_new_document_ids_in_folder(conn, folder_id)
    assignment = conn.execute(
        "SELECT a.status, u.display_name FROM folder_assignments a "
        "LEFT JOIN users u ON u.id = a.assigned_to "
        "WHERE a.folder_id = ? AND a.workflow = ?", (folder_id, workflow),
    ).fetchone() if _has_table(conn, "folder_assignments") else None
    exported = conn.execute(
        "SELECT exported_at FROM folder_exports WHERE folder_id = ? AND workflow = ?",
        (folder_id, workflow),
    ).fetchone() if _has_table(conn, "folder_exports") else None
    return {
        "folder_id": folder_id,
        "folder_display_id": folder_display(conn, folder_id)[0],
        "documents": len(document_ids),
        "document_ids": document_ids,
        "assigned_to": (assignment["display_name"] or "") if assignment else "",
        "status": assignment["status"] if assignment else "",
        "exported_at": exported["exported_at"] if exported else "",
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """The collaboration tables are created by accounts/collab, which a bare pipeline install never
    calls -- so every read of them has to cope with their absence."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def delete_new_document(conn: sqlite3.Connection, document_id: str) -> None:
    """Remove a new document and every workflow row owned by that document.

    The deletion log is intentionally owned elsewhere and survives this cleanup.
    """
    conn.execute("DELETE FROM new_document_pages WHERE document_id = ?", (document_id,))
    for table in ("document_reviews", "document_comments", "text_revisions", "page_orientations"):
        if _has_table(conn, table):
            conn.execute(
                f"DELETE FROM {table} WHERE document_id = ? AND workflow = 'new'",
                (document_id,),
            )
    conn.execute("DELETE FROM new_documents WHERE document_id = ?", (document_id,))


def get_new_documents_in_folder(conn: sqlite3.Connection, folder_id: str) -> list[sqlite3.Row]:
    """The id, display id and recorded cost of every document in one folder, in display order."""
    return conn.execute(
        "SELECT document_id, display_id, cost_estimate FROM new_documents "
        "WHERE folder_id = ? ORDER BY display_id", (folder_id,),
    ).fetchall()


def get_new_document_ids_in_folder(conn: sqlite3.Connection, folder_id: str) -> list[str]:
    """Every new_documents id belonging to one archival folder."""
    return [row["document_id"] for row in get_new_documents_in_folder(conn, folder_id)]


def page_orientations(conn: sqlite3.Connection, workflow: str, document_id: str) -> dict[int, dict]:
    """Each turned or mirrored page of a document, by its sequence index; an upright page is absent."""
    return {row["sequence_index"]: {"rotation": row["rotation"], "mirrored": bool(row["mirrored"])}
            for row in conn.execute(
                "SELECT sequence_index, rotation, mirrored FROM page_orientations "
                "WHERE workflow = ? AND document_id = ?", (workflow, document_id))}


def set_page_orientation(conn: sqlite3.Connection, workflow: str, document_id: str, sequence_index: int,
                         rotation: int, mirrored: bool, set_by: str | None) -> None:
    """Record a page's orientation; upright and unmirrored removes the row rather than storing a default."""
    if rotation == 0 and not mirrored:
        conn.execute("DELETE FROM page_orientations WHERE workflow = ? AND document_id = ? AND sequence_index = ?",
                     (workflow, document_id, sequence_index))
        return
    conn.execute(
        "INSERT INTO page_orientations (workflow, document_id, sequence_index, rotation, mirrored, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (workflow, document_id, sequence_index) DO UPDATE SET "
        "rotation = excluded.rotation, mirrored = excluded.mirrored, set_by = excluded.set_by, set_at = excluded.set_at",
        (workflow, document_id, sequence_index, rotation, int(mirrored), set_by,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))


def get_new_document_pages(conn: sqlite3.Connection, document_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM new_document_pages WHERE document_id = ? ORDER BY sequence_index",
        (document_id,),
    ).fetchall()


def get_new_document_status(conn: sqlite3.Connection, document_id: str) -> str | None:
    row = conn.execute("SELECT status FROM new_documents WHERE document_id = ?", (document_id,)).fetchone()
    return row["status"] if row else None


def count_new_pending_documents(conn: sqlite3.Connection, visible_folders: set[str] | None = None) -> int:
    """Count pending work in one query, returning folder totals instead of document identities."""
    if visible_folders is not None and not visible_folders:
        return 0
    rows = conn.execute(
        "SELECT folder_id, COUNT(*) AS total FROM new_documents "
        "WHERE status IN ('split_confirmed', 'error') GROUP BY folder_id"
    )
    return sum(row["total"] for row in rows
               if visible_folders is None or row["folder_id"] in visible_folders)


def get_new_pending_document_ids(conn: sqlite3.Connection, tab_id: int | None = None) -> list[str]:
    """Confirmed groups that haven't been through image-model drafting yet (or errored), site-wide or in one tab."""
    rows = conn.execute(
        "SELECT d.document_id FROM new_documents d LEFT JOIN folders f ON f.folder_id = d.folder_id "
        "WHERE d.status IN ('split_confirmed', 'error') AND (? IS NULL OR f.tab_id = ?) "
        "ORDER BY (d.status = 'error'), d.folder_id, d.display_id", (tab_id, tab_id)
    ).fetchall()
    return [row["document_id"] for row in rows]


def get_new_pending_document_ids_in_folder(conn: sqlite3.Connection, folder_id: str) -> list[str]:
    """Same as get_new_pending_document_ids, scoped to one folder."""
    rows = conn.execute(
        "SELECT document_id FROM new_documents "
        "WHERE status IN ('split_confirmed', 'error') AND folder_id = ? "
        "ORDER BY (status = 'error'), display_id", (folder_id,),
    ).fetchall()
    return [row["document_id"] for row in rows]


def upsert_new_document_ok(
    conn: sqlite3.Connection,
    document_id: str,
    model: str,
    provider: str,
    prompt_version_id: int | None,
    result: dict,
    title: str,
    output_json: dict,
    cost_estimate: float | None,
    duration_seconds: float | None = None,
    ocr_draft_text: str | None = "",
) -> None:
    """A finished draft: the transcription, its `fields` (keyed by the registry), and the title they give."""
    conn.execute(
        """
        UPDATE new_documents SET
            status = 'drafted', error_message = NULL,
            model = ?, provider = ?, prompt_version_id = ?, ai_title = ?,
            text = ?, uncertain_words_json = ?, fields_json = ?,
            ocr_draft_text = ?, output_json = ?, run_date = ?, cost_estimate = ?,
            duration_seconds = ?
        WHERE document_id = ?
        """,
        (
            model, provider, prompt_version_id, title,
            result["text"], json.dumps(result.get("uncertain_words", [])),
            json.dumps(result.get("fields", {}), ensure_ascii=False),
            ocr_draft_text, json.dumps(output_json, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(), cost_estimate, duration_seconds, document_id,
        ),
    )


def upsert_new_document_error(conn: sqlite3.Connection, document_id: str, error_message: str) -> None:
    conn.execute(
        "UPDATE new_documents SET status = 'error', error_message = ? WHERE document_id = ?",
        (error_message, document_id),
    )


def get_new_review_queue(conn: sqlite3.Connection, tab_id: int | None = None) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.document_id, d.display_id, d.folder_id, f.display_id AS folder_display_id,
               COALESCE(d.title_override, d.reviewed_title, d.ai_title) AS title, d.review_status,
               d.reviewer,
               COALESCE(json_extract(d.reviewed_fields_json, '$.challenges'),
                        json_extract(d.fields_json, '$.challenges')) AS challenges_json,
               (SELECT COUNT(*) FROM new_document_pages p WHERE p.document_id = d.document_id) AS page_count
        FROM new_documents d LEFT JOIN folders f ON f.folder_id = d.folder_id
        WHERE d.status = 'drafted' AND (? IS NULL OR f.tab_id = ?)
        ORDER BY f.display_id, d.display_id
        """, (tab_id, tab_id)
    ).fetchall()










def field_values(conn: sqlite3.Connection, row) -> tuple[list[dict], dict]:
    """(the document's registry, its effective field values): the reviewer's once saved, else the draft."""
    from . import fields, profiles

    registry = profiles.registry_for(conn, row)
    return registry, fields.effective(row, registry)


def effective_text(row) -> str:
    """The transcription as it stands: the reviewer's once saved, else the draft."""
    return row["reviewed_text"] if row["reviewed_text"] is not None else (row["text"] or "")


def signed_reviewer(row) -> str:
    """The reviewer's name, only while the document is accepted; a sent-back or unreviewed one names nobody."""
    return (row["reviewer"] or "").strip() if row["review_status"] == "accepted" else ""


def row_text(row, column: str) -> str:
    """A column that may not exist on an older database, as a string."""
    return (row[column] if column in row.keys() else "") or ""


def row_list(row, column: str) -> list:
    """One of the JSON-encoded list columns, as a list."""
    try:
        value = json.loads(row_text(row, column) or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def get_new_document_for_review(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM new_documents WHERE document_id = ? AND status = 'drafted'", (document_id,)
    ).fetchone()


def record_text_revisions(
    conn: sqlite3.Connection, document_id: str, workflow: str, new_values: dict,
    author_id: int | None, *, previous_values: dict | None = None, coalesce: bool = True,
    labels: dict | None = None,
) -> int:
    """Stores both sides of every real content edit before the write.

    ``previous_values`` is the effective value shown to the reviewer, so a first save compares
    against the draft the form displayed rather than an empty reviewed column. ``labels`` names
    each `field:<key>` as the document's registry does today.
    """
    from . import collab

    try:
        current = conn.execute(
            "SELECT * FROM new_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if current is None:
            return 0
        recorded = 0
        for field, after in new_values.items():
            if not collab.is_tracked(field):
                continue
            if previous_values is not None and field in previous_values:
                before = previous_values[field]
            else:
                before = current[field] if field in current.keys() else None
            if (collab.comparable_revision_value(field, before)
                    == collab.comparable_revision_value(field, after)):
                continue
            collab.record_revision(conn, document_id, workflow, field, before, after, author_id,
                                   coalesce=coalesce, label=(labels or {}).get(field))
            recorded += 1
        return recorded
    except sqlite3.Error:
        return 0


def save_new_review(
    conn: sqlite3.Connection,
    document_id: str,
    reviewed_title: str,
    title_override: str | None,
    reviewed_text: str,
    reviewed_fields: dict,
    review_status: str,
    reviewer: str,
    editors_notes: str,
) -> None:
    """`reviewed_fields` replaces the reviewer's field values wholesale: the form sends every field it shows."""
    conn.execute(
        """
        UPDATE new_documents
        SET reviewed_title = ?, title_override = ?, reviewed_text = ?, reviewed_fields_json = ?,
            review_status = ?, reviewer = ?, editors_notes = ?, reviewed_at = ?
        WHERE document_id = ?
        """,
        (reviewed_title, title_override, reviewed_text, json.dumps(reviewed_fields, ensure_ascii=False),
         review_status, reviewer, editors_notes, datetime.now(timezone.utc).isoformat(), document_id),
    )


def get_new_documents_for_export(
    conn: sqlite3.Connection, folder_id: str | None = None, include_unapproved: bool = False
) -> list[sqlite3.Row]:
    """Drafted documents in display-id order, for the CSV; fields are read with fields.effective."""
    where = "status = 'drafted'" if include_unapproved else "status = 'drafted' AND review_status = 'accepted'"
    params: tuple = ()
    if folder_id is not None:
        where += " AND folder_id = ?"
        params = (folder_id,)
    return conn.execute(
        f"""
        SELECT document_id, display_id, folder_id, prompt_version_id, fields_json, reviewed_fields_json,
               COALESCE(title_override, reviewed_title, ai_title) AS title,
               COALESCE(reviewed_text, text) AS text,
               editors_notes,
               -- A name belongs to an accepted document, never to one exported unapproved.
               CASE WHEN review_status = 'accepted' THEN reviewer ELSE '' END AS reviewer
        FROM new_documents
        WHERE {where}
        ORDER BY display_id
        """, params
    ).fetchall()


def get_new_document_folders(conn: sqlite3.Connection, tab_id: int | None = None) -> list[dict]:
    """Every folder holding at least one document, with its counts, for the folder browser."""
    folders: dict[str, dict] = {}
    for folder in conn.execute(
            "SELECT f.folder_id, f.display_id, f.name, f.uploaded_name, f.tab_id, t.slug AS tab_slug, "
            "t.label AS tab_label FROM folders f LEFT JOIN tabs t ON t.id = f.tab_id "
            "WHERE (? IS NULL OR f.tab_id = ?)", (tab_id, tab_id)):
        folders[folder["folder_id"]] = {
            "folder_id": folder["folder_id"],
            "tab_id": folder["tab_id"],
            "tab_slug": folder["tab_slug"] or "",
            "tab_label": folder["tab_label"] or "",
            "folder_display_id": folder["display_id"],
            "folder_name": folder["name"] or folder["display_id"],
            # What the folder is called on screen and in exports: FolderID_Group.
            "folder_title": docid.folder_directory(folder["display_id"], folder["name"])
                            or folder["display_id"],
            "default_folder_name": folder["uploaded_name"] or "",
            "document_count": 0,
            "drafted_count": 0,
            "accepted_count": 0,
            "awaiting_drafting_count": 0,
            # What drafting this folder has actually cost.
            "cost_estimate": 0.0,
        }
    for row in conn.execute(
            "SELECT folder_id, status, review_status, cost_estimate FROM new_documents"):
        f = folders.get(row["folder_id"])
        if f is None:
            continue
        f["document_count"] += 1
        f["cost_estimate"] += row["cost_estimate"] or 0.0
        if row["status"] == "drafted":
            f["drafted_count"] += 1
            if row["review_status"] == "accepted":
                f["accepted_count"] += 1
        if row["status"] in ("split_confirmed", "error"):
            f["awaiting_drafting_count"] += 1
    listed = [f for f in folders.values() if f["document_count"]]
    for f in listed:
        f["all_approved"] = f["accepted_count"] == f["document_count"]
    return sorted(listed, key=lambda f: (f["folder_display_id"].upper(), f["folder_id"]))


# Tables, child-before-parent, that make up "documents" as a reviewer thinks of them -- everything
# a fresh checkout starts with none of.
_DOCUMENT_TABLES = (
    "new_document_pages", "new_documents",
    "new_batch_images", "new_batches", "folders",
)


def reset_all_documents(conn: sqlite3.Connection) -> dict[str, int]:
    """Deletes every row from every document table, for the dev "start fresh" reset script, which
    pairs it with BucketStorage.delete_all()."""
    counts = {}
    for table in _DOCUMENT_TABLES:
        counts[table] = conn.execute(f"DELETE FROM {table}").rowcount
    return counts



