"""FastAPI backend for the review UI: the upload, split, draft, review and export routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import threading
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (access_routes, accounts, activity, backends, backup, collab, datafolder, db, docid, export_archive, fields,
               backup_routes, jobs, ledger, model_routes, update_routes, updates,
               file_locks, folder_exports, gaps, imaging, localname, openrouter, portmap, tls, profile_routes, profiles,
               scoping, system_routes, tab_routes, tabs)
from . import host as host_mod
from .assets import APP_STATIC_DIR
from .config import DEFAULT_PRODUCT_NAME, Settings
from .merge import merge_documents, plan_merge, renumber_folder
from .new_document import (NewDocumentOptions, retitle, title_for_row,
                           refresh_output_record, run_transcribe_new_documents)
from .split import (
    collect_resplit_pages,
    confirm_batch_split,
    delete_batch,
    resplit_documents,
    run_ingest_batch,
    run_propose_split,
    set_batch_folder_id,
    suggest_document_ids,
    suggest_more_document_ids,
)
from . import storage as storage_mod
from .storage import BucketStorage

logger = logging.getLogger(__name__)

# The most documents one folder-draft request will transcribe before returning and letting the
# client ask for more.
MAX_DOCUMENTS_PER_DRAFT_REQUEST = 5

# What a reviewer may set from the document screen. `needs_work` is not here: it is left by
# sending a folder back, never chosen on a document.
REVIEW_STATUSES = ("unreviewed", "accepted")


class NoCacheStaticFiles(StaticFiles):
    """This is a small local review tool, not a high-traffic site -- the bandwidth cost of never
    caching index.html/app.js/style.css is negligible, and it avoids reviewers seeing a stale UI
    after an update without knowing to hard-refresh (bit us during development: browsers were
    aggressively reusing a cached app.js across reloads with no way to tell from the UI)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


def _counts_down(entry, still_to_come: int = 0):
    """A progress callback for the drafting runners: keeps the job's own remaining count live, so
    two jobs running at once each report their own work rather than a single global number."""
    def report(remaining: int, _document_id: str) -> None:
        # A run the client sends in chunks counts down the whole run, not this chunk of it.
        activity.set_remaining(entry, remaining + still_to_come)
    return report


def _drafting_label(settings: Settings, what: str) -> str:
    """What the progress banner says while a draft is running."""
    return f"Transcribing {what} ({settings.openrouter_model})"


# The table carrying a workflow's own review_status: the admin's verdict, which export and the
# approval gate read.
def _review_table(workflow: str) -> str:
    return "new_documents"


def _draft_key(workflow: str, folder_id: str) -> str | None:
    """Which drafting runs collide with this one: one folder collides only with itself, so several
    folders can draft at once, and None (any folder's documents) collides with everything."""
    return f"new:{folder_id}" if folder_id else None


def _folder_of_ids(conn, document_ids: list[str]) -> str:
    """The one folder a set of documents belongs to, or "" if they somehow disagree."""
    folders = {db.folder_of_document(conn, document_id) for document_id in document_ids}
    return (folders.pop() or "") if len(folders) == 1 else ""


def _blank(value) -> str:
    """Render missing/empty metadata as '' rather than None."""
    return value if value else ""


# Shortest reason we'll accept.
MIN_DELETION_REASON_LENGTH = 4

# Guards a mis-picked folder (the picker aimed at a whole drive), not real work -- the largest real
# folders seen here run 70-91 scans.
MAX_UPLOAD_FILES = 200

# A real 90-scan archival folder can exceed the old 500 MB cap, but an unbounded multipart request
# can fill the host disk before Pillow ever sees an image. These deliberately generous ceilings
# reject abuse/mistakes while leaving roughly 22 MB per page across a maximum-sized batch.
MAX_UPLOAD_FILE_BYTES = 128 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 2 * 1024 * 1024 * 1024



# Multipart is parsed -- and spooled to the host's temp disk -- before any route body runs, so the
# per-scan and per-folder checks below cannot be what stops an oversized request. This ceiling is
# the declared request size, refused before a byte is read; it sits above MAX_UPLOAD_TOTAL_BYTES by
# enough to cover multipart boundaries and part headers.
MAX_REQUEST_BYTES = MAX_UPLOAD_TOTAL_BYTES + 32 * 1024 * 1024

MAX_PRESENCE_BYTES = 8 * 1024

# Where a body large enough to matter is allowed to arrive at all: (route, what the whole request
# may weigh, what the reviewer was told the payload limit is). The two differ by the multipart
# overhead, and a refusal quotes the second because that is the number on the screen. A route not
# named here gets MAX_REQUEST_BYTES and is not required to declare a length.
BODY_ROUTES = (
    ("/api/new/batches", MAX_REQUEST_BYTES, MAX_UPLOAD_TOTAL_BYTES),
    ("/api/presence", MAX_PRESENCE_BYTES, MAX_PRESENCE_BYTES),
)


class RequestSizeLimit:
    """Refuses an oversized request on its declared length, before Starlette reads the body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") in ("POST", "PUT", "PATCH"):
            refusal = self._refusal(scope)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _refusal(scope) -> JSONResponse | None:
        raw = dict(scope.get("headers") or []).get(b"content-length")
        path = scope.get("path", "")
        limits = next(((ceiling, stated) for route, ceiling, stated in BODY_ROUTES
                       if path.startswith(route)), None)
        if raw is None:
            # Only where a body big enough to matter is expected: everything else may be chunked,
            # and a browser always declares the length of a file upload.
            if limits is not None:
                return JSONResponse(
                    {"detail": "uploads must declare their size (Content-Length)"},
                    status_code=411)
            return None
        try:
            declared = int(raw)
        except ValueError:
            return JSONResponse({"detail": "malformed Content-Length"}, status_code=400)
        ceiling, stated = limits or (MAX_REQUEST_BYTES, MAX_UPLOAD_TOTAL_BYTES)
        if declared > ceiling:
            if stated < 1024 * 1024:
                limit_label = f"{stated // 1024} KB"
            else:
                limit_label = f"{stated // (1024 * 1024)} MB"
            return JSONResponse(
                {"detail": f"the request exceeds the {limit_label} limit"},
                status_code=413)
        return None




# Skipped without their bytes ever being read.
SKIP_UPLOAD_SUFFIXES = frozenset({
    ".pdf", ".zip", ".7z", ".rar", ".tar", ".gz",            # containers / compilations
    ".db", ".ini", ".lnk", ".txt", ".csv", ".xml", ".json",  # OS junk + sidecar metadata
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",       # office documents
})


class _LazyUploads:
    """A sized sequence of (filename, bytes) that reads each file only when the ingest loop reaches
    it, and skips known non-image files without reading them at all."""

    def __init__(self, uploads: list[UploadFile]):
        self._uploads = uploads
        # Lets the caller explain an empty batch -- otherwise a folder holding only a compilation
        # PDF fails with "no files uploaded", as though the upload never arrived.
        self.skipped: list[str] = []

    def __len__(self) -> int:
        return len(self._uploads)

    def __iter__(self):
        self.skipped = []
        for i, upload in enumerate(self._uploads):
            filename = upload.filename or f"page{i:04d}.jpg"
            if Path(filename).suffix.lower() in SKIP_UPLOAD_SUFFIXES:
                logger.info("upload: skipping %s -- not a scan page, not read", filename)
                self.skipped.append(filename)
                continue
            # Sync .read() (not await) -- see post_new_batch's note on why this handler is a plain
            # `def`; the file is fully received before this function runs either way.
            yield (filename, upload.file.read())


def _upload_size(upload: UploadFile) -> int:
    """Size without loading a spooled upload into memory, preserving its read position."""
    if upload.size is not None:
        return int(upload.size)
    position = upload.file.tell()
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(position)
    return size


def _validate_upload_sizes(uploads: list[UploadFile]) -> None:
    total = 0
    for upload in uploads:
        filename = upload.filename or "unnamed scan"
        if Path(filename).suffix.lower() in SKIP_UPLOAD_SUFFIXES:
            continue
        size = _upload_size(upload)
        if size > MAX_UPLOAD_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{filename} exceeds the {MAX_UPLOAD_FILE_BYTES // (1024 * 1024)} MB per-scan limit",
            )
        total += size
        if total > MAX_UPLOAD_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=("the scan folder exceeds the "
                        f"{MAX_UPLOAD_TOTAL_BYTES // (1024 * 1024)} MB upload limit"),
            )


def _deletion_context(request: Request, reason: str) -> tuple[str, str]:
    """Return the authenticated actor and a validated deletion reason.

    The actor is deliberately never accepted from request data: a query parameter is forgeable,
    whereas the signed-in session is already the site's identity boundary.
    """
    reviewer = _reviewer_name(request)
    reason = (reason or "").strip()
    if not reviewer:
        raise HTTPException(status_code=401, detail="sign in to delete")
    if len(reason) < MIN_DELETION_REASON_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"a reason of at least {MIN_DELETION_REASON_LENGTH} characters is required to delete",
        )
    return reviewer, reason


class ColourPayload(BaseModel):
    colour: str


class OrientationPayload(BaseModel):
    rotation: int = 0
    mirrored: bool = False


class RenameDocumentPayload(BaseModel):
    # The new display id; the document's internal id never changes.
    new_display_id: str


class NewDocumentGroup(BaseModel):
    display_id: str
    sequence_indices: list[int]


class ConfirmSplitPayload(BaseModel):
    groups: list[NewDocumentGroup]


class ResplitPayload(BaseModel):
    # The already-confirmed documents being replaced, and the boundaries to replace them with.
    document_ids: list[str]
    groups: list[NewDocumentGroup]


class MergePayload(BaseModel):
    # The already-drafted documents to rejoin into one.
    document_ids: list[str]


class SuggestMoreDocIdsPayload(BaseModel):
    folder_id: str
    count: int
    exclude: list[str] = []


class SetBatchFolderIdPayload(BaseModel):
    # The folder's display id as typed ("E2002"); the server finds or creates the folder.
    folder_display_id: str
    # Optional -- blank leaves the folder's already-stored name (if any) untouched, so correcting
    # just the id here never erases a name captured correctly at upload time.
    folder_name: str = ""


class SetFolderNamePayload(BaseModel):
    folder_name: str = ""


class AppSettingsPayload(BaseModel):
    max_total_cost: float
    new_document_batch_size: int


class NewReviewPayload(BaseModel):
    # The reviewer's title from the (now editable) Title field.
    title: str = ""
    reviewed_text: str
    # Keyed by the document's registry (fields.validate_review checks the keys and shapes).
    fields: dict = {}
    review_status: str
    reviewer: str = ""
    editors_notes: str = ""
    # Changes only when Redraft replaces the underlying model draft.
    draft_version: str | None = None


def _viewer(request) -> object | None:
    """The signed-in user for this request, or None."""
    return getattr(request.state, "user", None)


def _comment_counts(counts: dict, document_id: str) -> dict:
    """A document's comment tally, which both roles see -- comments are how either of them says a
    document needs another look."""
    found = counts.get(document_id) or {}
    return {"comments": found.get("total", 0), "open_comments": found.get("open", 0)}


def _verdicts(row, role: str, reviewers: dict) -> dict:
    """The two verdicts a folder row draws: the admin's, and the reviewer's."""
    if role != "admin":
        return {}
    reviewer = reviewers.get(row["document_id"]) or {}
    return {
        "reviewed_by": db.signed_reviewer(row),
        "reviewer_status": reviewer.get("status", "unreviewed"),
        "reviewer_name": reviewer.get("reviewer", ""),
    }


def _with_reviewers(conn, folders: list[dict], workflow: str) -> list[dict]:
    """Each folder's reviewers, how many documents each marked reviewed, and their colour."""
    tally = db.reviewed_by_folder(conn, workflow)
    colours = accounts.reviewer_colours(conn)
    people = {row["display_name"]: row["id"] for row in
              conn.execute("SELECT id, display_name FROM users WHERE role IN ('owner', 'admin')")}
    palette = {key: (bg, ink) for key, bg, ink in accounts.REVIEWER_COLOURS}
    for folder in folders:
        names = tally.get(folder.get("folder_id", ""), {})
        reviewers = []
        for name, count in names.items():
            user_id = people.get(name)
            bg, ink = palette.get(colours.get(user_id), ("", ""))
            reviewers.append({"name": name, "count": count, "user_id": user_id,
                              "colour": colours.get(user_id), "bg": bg, "ink": ink})
        folder["reviewers"] = sorted(reviewers, key=lambda r: (-r["count"], r["name"]))
    return folders


def _reviewer_name(request) -> str:
    """Who is doing the reviewing, taken from the login rather than from a field they fill in."""
    viewer = _viewer(request)
    if viewer is None:
        return ""
    for key in ("display_name", "username"):
        try:
            value = (viewer[key] or "").strip()
        except (KeyError, IndexError, TypeError):
            value = ""
        if value:
            return value
    return ""


def _sync_app_settings(conn, settings: Settings) -> dict:
    """Refreshes settings.max_total_cost from the reviewer-adjustable Settings menu
    (db.app_settings) and returns the full row -- called at the top of every route that starts a
    costed batch or needs a pacing value, so a change made in the Settings menu takes effect on"""
    app_settings = db.get_app_settings(conn)
    settings.max_total_cost = app_settings["max_total_cost"]
    settings.monthly_cap = app_settings["monthly_cap"]
    return app_settings


def create_app(
    settings: Settings,
    export_dir: str = "data/exports",
    backup_status_dir: str = "deploy/logs",
    backup_dir: str | None = None,
    backup_mirror: str | None = None,
) -> FastAPI:
    # Schema migrations (db._migrate) run here, so an older database picks up new columns.
    db.init_db(settings.db_path)
    # Whatever a previous run died holding, before this one starts making more.
    export_archive.migrate_legacy(export_dir)
    export_jobs = folder_exports.Jobs(settings, export_dir, backup_dir, backup_mirror)

    app = FastAPI(title=f"{DEFAULT_PRODUCT_NAME} Review")
    app.add_middleware(RequestSizeLimit)
    storage = BucketStorage(settings.bucket_uri)
    system_routes.install(app, settings, backup_status_dir, _viewer)

    @app.post("/api/folder-exports/{workflow}/{folder_id}", status_code=202)
    def start_folder_export(request: Request, workflow: str, folder_id: str):
        try:
            job_id = export_jobs.start(workflow, folder_id, _viewer_name(request))
        except (ValueError, collab.NotReady) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "state": "queued"}

    @app.get("/api/folder-exports/jobs/{job_id}")
    def folder_export_status(job_id: str):
        result = export_jobs.status(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="export job not found")
        return result

    def _require_document_access(conn, request: Request, document_id: str,
                                 workflow: str) -> None:
        """Enforce the folder boundary on direct document URLs, not only on queue listings."""
        folder_id = scoping.document_folder_id(conn, document_id, workflow)
        if folder_id is None:  # The route itself supplies its normal 404.
            return
        visible = scoping.visible_folder_ids(conn, _viewer(request), workflow)
        if visible is not None and folder_id not in visible:
            raise HTTPException(status_code=403, detail="that document is not assigned to you")

    def _locked_for(conn, request: Request, document_id: str, workflow: str) -> bool:
        """Whether this viewer is a reviewer looking at a document whose folder is approved."""
        viewer = _viewer(request)
        if viewer is None or viewer["role"] != "reviewer":
            return False
        folder_id = scoping.document_folder_id(conn, document_id, workflow)
        assignment = accounts.get_assignment(conn, folder_id, workflow) if folder_id else None
        return assignment is not None and assignment["status"] == "approved"

    def _require_tab_open(conn, tab_id) -> None:
        """Refuse work in a paused tab, naming the tab."""
        try:
            tabs.require_open(conn, tab_id)
        except tabs.TabConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _require_folder_tab_open(conn, folder_id) -> None:
        try:
            tabs.require_folder_open(conn, folder_id)
        except tabs.TabConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _require_folder_open(conn, request: Request, document_id: str, workflow: str) -> None:
        """Refuse a change to a document whose folder is approved (a reviewer's lock) or whose tab is paused."""
        if _locked_for(conn, request, document_id, workflow):
            raise HTTPException(status_code=409,
                                detail="this folder is approved, so its documents are locked")
        _require_folder_tab_open(conn, scoping.document_folder_id(conn, document_id, workflow))

    def _require_folder_access(conn, request: Request, folder_id: str,
                               workflow: str) -> None:
        """Enforce folder ownership on routes that do not start from a document row."""
        visible = scoping.visible_folder_ids(conn, _viewer(request), workflow)
        if visible is not None and folder_id not in visible:
            raise HTTPException(status_code=403, detail="that folder is not assigned to you")

    def _require_batch_access(conn, request: Request, batch_id: str):
        """Return a batch only when the viewer uploaded it or may access its folder."""
        batch = db.get_batch(conn, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        visible = scoping.visible_folder_ids(conn, _viewer(request), "new")
        if visible is None:
            return batch
        viewer = _viewer(request)
        if viewer is not None and batch["uploaded_by"] == viewer["id"]:
            return batch
        if (batch["folder_id"] or "") in visible:
            return batch
        raise HTTPException(status_code=403, detail="that batch does not belong to you")

    def _refuse_another_spelling(conn, display_id: str, tab_id: int = db.DEFAULT_TAB_ID) -> None:
        """Refused for everyone, replace or not: one folder, one spelling, within its tab."""
        other = db.folder_spelled_otherwise(conn, display_id, tab_id) if display_id else None
        if other is not None:
            raise HTTPException(status_code=409, detail=f"{display_id} is already on the site as {other}; "
                                                         f"upload it as {other} if it is the same folder")

    def _require_folder_available_for_upload(conn, request: Request, display_id: str,
                                             tab_id: int = db.DEFAULT_TAB_ID) -> None:
        """Prevent a reviewer from attaching their scans to a folder somebody else owns."""
        _refuse_another_spelling(conn, display_id, tab_id)
        viewer = _viewer(request)
        if not display_id or viewer is None or scoping.is_admin(viewer):
            return
        folder = db.find_folder(conn, display_id, tab_id)
        if folder is None:
            return
        folder_id = folder["folder_id"]
        assignment = accounts.get_assignment(conn, folder_id, "new")
        if assignment is not None and assignment["assigned_to"] != viewer["id"]:
            raise HTTPException(status_code=403, detail="that folder belongs to another reviewer")
        other_upload = conn.execute(
            "SELECT 1 FROM new_batches WHERE folder_id = ? AND uploaded_by IS NOT NULL "
            "AND uploaded_by <> ? LIMIT 1", (folder_id, viewer["id"])
        ).fetchone()
        if other_upload is not None:
            raise HTTPException(status_code=403, detail="that folder was uploaded by another reviewer")

    def _require_documents_access(conn, request: Request, document_ids: list[str],
                                  workflow: str) -> None:
        """Require every member of a merge/resplit selection, not merely its first document."""
        for document_id in dict.fromkeys(document_ids):
            _require_document_access(conn, request, document_id, workflow)

    def _new_document_lock(document_id: str, *, shared: bool):
        try:
            with activity.resource_lock(f"new-document:{document_id}", shared=shared):
                yield
        except activity.ResourceBusy:
            raise HTTPException(
                status_code=409,
                detail="this document is being redrafted or saved; reopen it and try again",
            ) from None

    def _lock_new_document_save(document_id: str):
        """Allow concurrent saves, but never while a redraft owns the document."""
        yield from _new_document_lock(document_id, shared=True)

    def _lock_new_document_redraft(document_id: str):
        """Own the document exclusively until its replacement draft is durable."""
        yield from _new_document_lock(document_id, shared=False)

    def _serve_page_image(request: Request, bucket_key: str, size: str) -> Response:
        """One scan, at the size the screen asked for, cached by the browser."""
        if size not in imaging.DISPLAY_SIZES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown size {size!r}; expected one of {sorted(imaging.DISPLAY_SIZES)}",
            )

        try:
            source_size = storage.size(bucket_key)
        except (FileNotFoundError, OSError):
            raise HTTPException(status_code=404, detail="page image not found") from None

        # Identifies the bytes without reading them: a stored scan is immutable, so its key, its
        # length and which rendering was asked for say everything about what comes back.
        etag = '"' + hashlib.sha256(f"{bucket_key}|{size}|{source_size}".encode()).hexdigest()[:32] + '"'
        headers = {
            "ETag": etag,
            # Long, because the content behind a given key genuinely never changes -- but
            # revalidated rather than blindly reused, so a bucket rebuilt underneath the same key
            # can't serve a stale page for a week.
            "Cache-Control": "private, max-age=86400, must-revalidate",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)

        content = imaging.cached_render(storage, bucket_key, size)
        media_type = "image/jpeg"
        if size == "full" and not bucket_key.lower().endswith((".jpg", ".jpeg")):
            media_type = "image/png"  # a reduced page is always re-encoded to JPEG; a full one is as stored
        return Response(content=content, media_type=media_type, headers=headers)

    @app.exception_handler(activity.AlreadyRunning)
    def _already_running(request, exc: activity.AlreadyRunning):
        """409, not 500: a second reviewer clicking Draft while a draft is running is ordinary use,
        not an error in the request."""
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"{exc.running.get('label') or exc.kind} is already running. "
                          "Wait for it to finish, then try again.",
                "kind": exc.kind,
                "running": exc.running.get("label", ""),
            },
        )

    @app.get("/api/analytics/folder")
    def folder_analytics(request: Request, folder_id: str, workflow: str = "new"):
        """What drafting one folder cost and how long it took."""
        if workflow not in accounts.WORKFLOWS:
            raise HTTPException(status_code=400, detail="unknown workflow")
        with db.connect(settings.db_path) as conn:
            _require_folder_access(conn, request, folder_id, workflow)
            summary = db.get_new_folder_analytics(conn, folder_id)
        if not summary["document_count"]:
            raise HTTPException(status_code=404, detail="no such folder")
        return {"folder_id": folder_id, "workflow": workflow, **summary}

    def _save_orientation(request: Request, workflow: str, document_id: str, sequence_index: int,
                          payload: OrientationPayload) -> dict:
        """Whoever may edit the document may turn its pages; an approved folder's stay as they are for a reviewer."""
        if payload.rotation not in (0, 90, 180, 270):
            raise HTTPException(status_code=400, detail="rotation must be 0, 90, 180 or 270")
        with db.connect(settings.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _require_document_access(conn, request, document_id, workflow)
            pages = db.get_new_document_pages(conn, document_id)
            if not any(p["sequence_index"] == sequence_index and p["bucket_key"] for p in pages):
                raise HTTPException(status_code=404, detail="page not found")
            _require_folder_open(conn, request, document_id, workflow)
            db.set_page_orientation(conn, workflow, document_id, sequence_index,
                                    payload.rotation, payload.mirrored, _reviewer_name(request) or None)
        return {"rotation": payload.rotation, "mirrored": payload.mirrored}

    @app.put("/api/new/documents/{document_id}/pages/{sequence_index}/orientation")
    def put_new_page_orientation(request: Request, document_id: str, sequence_index: int,
                                 payload: OrientationPayload):
        return _save_orientation(request, "new", document_id, sequence_index, payload)

    def _own_status(row, request, workflow: str) -> str:
        """The status to show the person asking."""
        if _viewer_role(request) == "admin":
            return row["review_status"]
        with db.connect(settings.db_path) as conn:
            mine = db.get_document_review(conn, row["document_id"], workflow, _viewer_role(request))
        return mine or "unreviewed"

    def _status_to_store(conn, request, document_id: str, workflow: str, asked: str) -> str:
        """What to write to the document's own review_status column."""
        if _viewer_role(request) == "admin":
            return asked
        table = _review_table(workflow)
        row = conn.execute(
            f"SELECT review_status FROM {table} WHERE document_id = ?", (document_id,)
        ).fetchone()
        return row["review_status"] if row else "unreviewed"

    def _require_status_allowed(conn, request, document_id: str, workflow: str, asked: str) -> None:
        """`needs_work` may only be sent back unchanged, because Save keeps a status rather than choosing one."""
        if asked != collab.NEEDS_WORK:
            return
        if _viewer_role(request) == "admin":
            table = _review_table(workflow)
            row = conn.execute(
                f"SELECT review_status FROM {table} WHERE document_id = ?", (document_id,)).fetchone()
            current = row["review_status"] if row else None
        else:
            current = db.get_document_review(conn, document_id, workflow, _viewer_role(request))
        if current != collab.NEEDS_WORK:
            raise HTTPException(status_code=400, detail=f"review_status must be one of {REVIEW_STATUSES}")

    def _reviewer_to_store(conn, request, document_id: str, workflow: str, status: str) -> str:
        """Who to write to the document's own reviewer column."""
        # Keeping a send-back mark is not a new verdict, so it keeps the name already there.
        if _viewer_role(request) == "admin" and status != collab.NEEDS_WORK:
            # Only a verdict carries a name. Taking one back leaves the document unsigned, rather
            # than "reviewed by" whoever last opened it.
            return _reviewer_name(request) if status in collab.VERDICTS else ""
        table = _review_table(workflow)
        row = conn.execute(
            f"SELECT reviewer FROM {table} WHERE document_id = ?", (document_id,)).fetchone()
        return (row["reviewer"] or "") if row else ""

    def _record_role_review(conn, request, document_id: str, workflow: str, status: str,
                            reviewer: str) -> None:
        """The asking role's own verdict, kept separately from the document's shared column."""
        # Same rule as the shared column: a name belongs to a verdict, not to opening the document.
        db.set_document_review(conn, document_id, workflow, _viewer_role(request), status,
                               reviewer if status in collab.VERDICTS else "")

    def _viewer_name(request) -> str:
        """Display name of whoever is asking, or "" when nobody is signed in."""
        viewer = _viewer(request)
        if viewer is None:
            return ""
        try:
            return viewer["display_name"] or ""
        except (KeyError, IndexError):
            return ""

    # Progress callbacks a waiting route lends the job it runs, so its activity bar counts down.
    live_progress: dict[int, object] = {}

    def _draft_handler(job: dict, progress) -> dict:
        """Drafts whatever of the job's documents is still pending, so a re-run never drafts twice."""
        with db.connect(settings.db_path) as conn:
            _sync_app_settings(conn, settings)
            pending = set(db.get_new_pending_document_ids(conn))
        ids = [d for d in job["document_ids"] if d in pending]
        total = job["total"] or len(ids)
        lent = live_progress.get(job["id"])

        def report(remaining: int, document_id: str) -> None:
            if lent is not None:
                lent(remaining, document_id)
            progress(done=total - remaining, current=document_id)
        try:
            outcome = run_transcribe_new_documents(
                settings, NewDocumentOptions(max_total_cost=settings.max_total_cost, monthly_cap=settings.monthly_cap),
                document_ids=ids, progress=report)
        except openrouter.AccountError as exc:
            raise jobs.AccountProblem(str(exc)) from exc
        with db.connect(settings.db_path) as conn:
            still = set(db.get_new_pending_document_ids(conn))
        jobs._update(settings.db_path, job["id"], done=total - len([d for d in job["document_ids"] if d in still]))
        return outcome or {}

    def _split_handler(job: dict, progress) -> dict:
        with db.connect(settings.db_path) as conn:
            _sync_app_settings(conn, settings)
        run_propose_split(job["batch_id"], settings)
        return {}

    job_handlers = {"draft": _draft_handler, "split": _split_handler}

    def _draft_job(request, document_ids: list[str], folder_id: str | None, progress=None) -> dict:
        """Enqueues these documents' draft and runs it here, or waits for whoever already is."""
        job = jobs.enqueue(settings.db_path, "draft", folder_id=folder_id, document_ids=document_ids,
                           label=f"Drafting {len(document_ids)} document(s)", requested_by=_owner_id(request))
        if progress is not None:
            live_progress[job["id"]] = progress
        try:
            return jobs.run_and_wait(settings.db_path, job_handlers, job)
        finally:
            live_progress.pop(job["id"], None)

    def _terminal(job: dict) -> str:
        """What stops the run for everyone: the key, the balance, or a cap."""
        if job["state"] == "failed" and job["error_kind"] == "account":
            return job["error_text"]
        if job["state"] == "failed":
            raise HTTPException(status_code=500, detail=job["error_text"] or "drafting failed")
        cap = (job.get("result") or {}).get("stopped_by_cap")
        return f"Stopped: {cap}." if cap else ""

    def _owner_id(request):
        viewer = _viewer(request)
        try:
            return viewer["id"] if viewer is not None else None
        except (KeyError, IndexError):
            return None

    def _viewer_role(request) -> str:
        """Whose review this request is about."""
        viewer = _viewer(request)
        # Fail closed: the gate always sets a viewer, so a missing one is a bug, not an admin.
        if viewer is None:
            raise HTTPException(status_code=401, detail="sign in to continue")
        return "admin" if scoping.is_admin(viewer) else "reviewer"

    def _project_status(conn, rows: list[dict], workflow: str, role: str) -> list[dict]:
        """Replaces each row's review_status with the asking role's own."""
        if role == "admin":
            return rows
        mine = db.get_document_reviews(conn, workflow, role)
        return [{**row, "review_status": mine.get(row["document_id"], "unreviewed")} for row in rows]

    def _with_export_state(conn, folders: list[dict], workflow: str) -> list[dict]:
        """Attaches each folder's last export, so the queue can tell a folder that has gone out
        from one that still has to."""
        exports = db.get_folder_exports(conn, workflow)
        claims = db.get_folder_claims(conn, workflow)
        return [
            {
                **folder,
                "export": exports.get(folder.get("folder_id") or ""),
                "claim": claims.get(folder.get("folder_id") or ""),
            }
            for folder in folders
        ]


    @app.get("/api/settings")
    def get_settings():
        """Powers the Settings menu -- the reviewer-adjustable pacing/cost knobs (db.app_settings)
        plus current all-time spend, so the menu can show e.g. "$0.13 of $2.00 used" alongside
        the editable cap rather than the cap in isolation."""
        with db.connect(settings.db_path) as conn:
            app_settings = db.get_app_settings(conn)
            total_cost = db.get_total_cost(conn)
        return {**app_settings, "total_cost": total_cost}

    @app.post("/api/settings")
    def post_settings(payload: AppSettingsPayload):
        if payload.new_document_batch_size < 1:
            raise HTTPException(status_code=400, detail="batch sizes must be at least 1")
        if payload.max_total_cost <= 0:
            raise HTTPException(status_code=400, detail="max total cost must be greater than 0")
        with db.connect(settings.db_path) as conn:
            db.update_app_settings(
                conn,
                max_total_cost=payload.max_total_cost,
                new_document_batch_size=payload.new_document_batch_size,
            )
            app_settings = db.get_app_settings(conn)
            total_cost = db.get_total_cost(conn)
        settings.max_total_cost = app_settings["max_total_cost"]  # takes effect on the very next request
        return {**app_settings, "total_cost": total_cost}

    @app.get("/api/deletion-log")
    def get_deletion_log_endpoint(limit: int = 200):
        """Every recorded deletion, newest first -- what the review UI's "Deletion Log" tab shows."""
        with db.connect(settings.db_path) as conn:
            rows = db.get_deletion_log(conn, limit=max(1, min(limit, 1000)))
        return [
            {
                "id": row["id"],
                "deleted_at": row["deleted_at"],
                "reviewer": row["reviewer"],
                "reason": row["reason"],
                "workflow": row["workflow"],
                "target_kind": row["target_kind"],
                "target_id": row["target_id"],
                "summary": row["summary"],
                "cost_estimate": row["cost_estimate"],
                "details": json.loads(row["details_json"]) if row["details_json"] else None,
            }
            for row in rows
        ]

    # ---------- "Add New Document" workflow ----------

    def _new_folder_label(folder_id: str) -> str:
        """What to call a new-documents folder on a status bar, matching _batch_folder."""
        if not folder_id:
            return ""
        try:
            with db.connect(settings.db_path) as conn:
                return db.folder_title(conn, folder_id)
        except Exception:  # noqa: BLE001 - a label is never worth failing the work for
            return ""

    def _batch_folder(batch_id: str) -> str:
        """What to call the folder a batch belongs to, on a status bar."""
        try:
            with db.connect(settings.db_path) as conn:
                batch = db.get_batch(conn, batch_id)
                if batch is None:
                    return ""
                if batch["folder_id"]:
                    return db.folder_title(conn, batch["folder_id"])
                return db.row_text(batch, "uploaded_folder_name")
        except Exception:  # noqa: BLE001 - a label is never worth failing the work for
            return ""

    def _batch_payload(batch_id: str, proposal_error: str | None = None) -> dict:
        with db.connect(settings.db_path) as conn:
            batch = db.get_batch(conn, batch_id)
            if batch is None:
                raise HTTPException(status_code=404, detail="batch not found")
            images = db.get_batch_images(conn, batch_id)
            folder_display_id, folder_name = db.folder_display(conn, batch["folder_id"])
            display = {img["assigned_document_id"]: db.display_id_of(conn, img["assigned_document_id"])
                       for img in images if img["assigned_document_id"]}

        try:
            suggested_document_ids = suggest_document_ids(batch_id, settings)
        except Exception:  # noqa: BLE001 - a suggestion failure never blocks the split-review screen
            suggested_document_ids = []

        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "folder_id": batch["folder_id"] or "",
            "folder_display_id": folder_display_id,
            "folder_name": folder_name or db.row_text(batch, "uploaded_folder_name"),
            "proposal_error": proposal_error,
            # Lets the frontend tell "fully proposed" apart from "still has unexamined pages" --
            # e.g. to decide whether Retry/Continue Split Proposal should show on the split-review
            # screen even when there's no proposal_error (the reviewer navigated away mid-proposal
            # and came back via Resume, rather than the call actually failing).
            "image_count": batch["image_count"],
            "split_proposal_cursor": batch["split_proposal_cursor"] or 0,
            "suggested_document_ids": suggested_document_ids,
            "pages": [
                {
                    "sequence_index": img["sequence_index"],
                    "original_filename": img["original_filename"],
                    "proposed_new_document": bool(img["proposed_new_document"]),
                    "is_duplicate_scan": bool(img["is_duplicate_scan"]),
                    "assigned_document_id": img["assigned_document_id"],
                    "assigned_display_id": display.get(img["assigned_document_id"], ""),
                    "image_url": f"/api/new/batches/{batch_id}/images/{img['sequence_index']}",
                }
                for img in images
            ],
        }

    @app.get("/api/new/batches")
    def list_pending_batches(request: Request, folder_id: str | None = None, tab_id: int | None = None):
        """Batches uploaded (and possibly split-proposed) but never confirmed -- lets the split-
        review screen be resumed for one of these, e.g. after the reviewer's browser was never
        open for an upload done another way, or they navigated away before confirming."""
        with db.connect(settings.db_path) as conn:
            rows = db.get_pending_batches(conn, tab_id)
            if folder_id is not None:
                rows = [r for r in rows if (r["folder_id"] or "") == folder_id]
            payload = [
                {
                    "batch_id": row["batch_id"],
                    "status": row["status"],
                    "image_count": row["image_count"],
                    "split_proposal_cursor": row["split_proposal_cursor"] or 0,
                    "folder_id": row["folder_id"] or "",
                    "folder_display_id": db.folder_display(conn, row["folder_id"])[0],
                    "uploaded_at": row["uploaded_at"],
                }
                for row in rows
            ]
        # Narrowed and attributed: a reviewer sees only their own uploads, while an admin sees every
        # one along with who uploaded it -- which is how a new folder announces itself to the team.
        with db.connect(settings.db_path) as conn:
            return scoping.filter_batches(conn, _viewer(request), payload)

    @app.delete("/api/new/batches/{batch_id}")
    def delete_pending_batch(request: Request, batch_id: str, reason: str = ""):
        """Discards a pending (uploaded/split-proposed but never confirmed) batch entirely -- its
        intake images and its DB rows -- so it stops cluttering the "Awaiting split
        confirmation" panel."""
        reviewer, reason = _deletion_context(request, reason)
        # Read what the log entry needs while the rows still exist, but don't write it yet:
        # delete_batch refuses an already-confirmed batch, and a log full of deletions that were
        # rejected would be worse than no log at all.
        with db.connect(settings.db_path) as conn:
            row = db.get_batch(conn, batch_id)
            if row is None:
                raise HTTPException(status_code=404, detail="batch not found")
            folder_display_id = db.folder_display(conn, row["folder_id"])[0]
            summary = f"{folder_display_id or 'unnamed batch'} ({len(db.get_batch_images(conn, batch_id))} scans, {row['status']})"
            split_cost = row["split_cost_estimate"] or 0.0
            details = {"folder_id": row["folder_id"], "folder_display_id": folder_display_id,
                       "status": row["status"]}

        try:
            delete_batch(batch_id, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        with db.connect(settings.db_path) as conn:
            db.record_deletion(
                conn,
                reviewer=reviewer,
                reason=reason,
                workflow="new",
                target_kind="pending_batch",
                target_id=batch_id,
                summary=summary,
                cost_estimate=split_cost,
                details=details,
            )
        return {"ok": True}

    @app.post("/api/new/batches/{batch_id}/folder-id")
    def post_batch_folder_id(request: Request, batch_id: str, payload: SetBatchFolderIdPayload):
        """Sets or corrects a pending batch's Folder ID (and optionally its descriptive Folder
        Name) after upload -- e.g. the reviewer forgot to fill the id in before uploading,
        leaving every proposed group unnamed."""
        display_id = payload.folder_display_id.strip()
        with db.connect(settings.db_path) as conn:
            batch = _require_batch_access(conn, request, batch_id)
            _require_folder_available_for_upload(conn, request, display_id, batch["tab_id"])
            existing = db.find_folder(conn, display_id, batch["tab_id"]) if display_id else None
            if (_viewer_role(request) != "admin" and existing is not None
                    and db.folder_has_work(conn, existing["folder_id"], "new")["documents"]):
                raise HTTPException(status_code=409, detail="that folder already exists")
        try:
            set_batch_folder_id(batch_id, display_id, settings, folder_name=payload.folder_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _batch_payload(batch_id)

    @app.post("/api/new/folders/{folder_id}/name")
    def post_folder_name(request: Request, folder_id: str, payload: SetFolderNamePayload):
        """Renames a folder after its split has been confirmed."""
        if _viewer_role(request) != "admin":
            raise HTTPException(status_code=403, detail="only an editor can rename a folder")
        name = (payload.folder_name or "").strip()
        with db.connect(settings.db_path) as conn:
            if db.get_folder(conn, folder_id) is None:
                raise HTTPException(status_code=404, detail="no such folder")
            _, default = db.new_folder_names(conn, folder_id)
            # Blank means "use the default", which is what Use Default Title sends.
            name = name or default
            db.set_new_folder_name(conn, folder_id, name)
            # Nothing in the bucket or the export tree is named after the folder, so nothing moves.
            title = db.folder_title(conn, folder_id)
            folder_display_id = db.folder_display(conn, folder_id)[0]
        return {"ok": True, "folder_id": folder_id, "folder_display_id": folder_display_id,
                "folder_name": name, "folder_title": title, "default_folder_name": default}

    @app.post("/api/new/batches/{batch_id}/retry-proposal")
    def post_retry_proposal(request: Request, batch_id: str):
        """Runs (or continues) automatic split proposal for whatever part of this batch hasn't been
        proposed yet -- run_propose_split resumes from the batch's own split_proposal_cursor, so
        this never re-examines (or re-bills) pages already proposed successfully, even after 413
        shrinking"""
        with db.connect(settings.db_path) as conn:
            batch = _require_batch_access(conn, request, batch_id)
            _sync_app_settings(conn, settings)
        proposal_error = None
        try:
            with activity.exclusive("splitting", "Asking the model where each document starts",
                                    owner_id=_owner_id(request),
                                    folder=_batch_folder(batch_id)):
                job = jobs.enqueue(settings.db_path, "split", batch_id=batch_id,
                                   folder_id=db.row_text(batch, "folder_id") or None, label="Proposing a split",
                                   requested_by=_owner_id(request))
                finished = jobs.run_and_wait(settings.db_path, job_handlers, job)
            if finished["state"] == "failed":
                proposal_error = finished["error_text"]
        except Exception as exc:  # noqa: BLE001 - whatever succeeded so far is still kept; see post_new_batch
            proposal_error = str(exc)
        return _batch_payload(batch_id, proposal_error)

    def _replacement_warning(existing: dict) -> str:
        """Said in full, because what it costs is not recoverable: the transcriptions, both roles'
        verdicts and the folder's whole history go when it is replaced."""
        parts = [f"{existing['folder_display_id']} already holds {existing['documents']} document(s)"]
        if existing["assigned_to"]:
            parts.append(f"assigned to {existing['assigned_to']}")
        if existing["status"]:
            parts.append(f"currently {existing['status']}")
        if existing["exported_at"]:
            parts.append(f"exported {existing['exported_at'][:10]}")
        return (
            ", ".join(parts)
            + ". Uploading it again permanently discards those transcriptions, every reviewer and "
              "reviewer verdict on them, and the folder's history. This cannot be undone."
        )

    def _delete_folder_uploads(folder_id: str) -> int:
        """The raw scans of every batch of a folder."""
        with db.connect(settings.db_path) as conn:
            folders = [db.row_text(row, "bucket_folder") for row in conn.execute(
                "SELECT bucket_folder FROM new_batches WHERE folder_id = ?", (folder_id,))]
        removed = 0
        for bucket_folder in {f for f in folders if f}:
            storage.delete_prefix(storage_mod.UPLOADS_PREFIX, bucket_folder)
            removed += 1
        return removed

    folder_upload_locks: dict[str, dict] = {}
    folder_upload_locks_guard = threading.Lock()

    @contextmanager
    def _folder_upload_lock(folder_id: str):
        """Serialise case variants across threads and server processes sharing this database."""
        if not folder_id:
            yield                   # an ungrouped upload collides with nobody
            return
        lock_key = folder_id.casefold()
        with folder_upload_locks_guard:
            slot = folder_upload_locks.setdefault(
                lock_key, {"lock": threading.Lock(), "users": 0})
            slot["users"] += 1      # includes waiters, so their lock cannot be replaced underneath them
        try:
            with slot["lock"]:
                database = Path(settings.db_path).resolve()
                path = (database.parent / ".folder-upload-locks" / database.name /
                        f"{lock_key}.lock")
                with file_locks.hold(path, wait=True):
                    yield
        finally:
            with folder_upload_locks_guard:
                slot["users"] -= 1
                if slot["users"] == 0 and folder_upload_locks.get(lock_key) is slot:
                    folder_upload_locks.pop(lock_key, None)

    def _folder_before_replacement(folder_id: str) -> dict:
        """Exactly what the folder holds now, so the replacement upload can be ingested first and
        only this -- never the new scans -- discarded once it has landed."""
        with db.connect(settings.db_path) as conn:
            document_rows = db.get_new_documents_in_folder(conn, folder_id)
            document_ids = [row["document_id"] for row in document_rows]
            batch_ids, scan_keys, batch_costs = [], [], {}
            # One joined query rather than one image query per historic batch. A replacement folder
            # should normally have one batch, but this path also repairs older duplicated states.
            for row in conn.execute(
                    "SELECT b.batch_id, b.split_cost_estimate, i.bucket_key "
                    "FROM new_batches b LEFT JOIN new_batch_images i ON i.batch_id = b.batch_id "
                    "WHERE b.folder_id = ? ORDER BY b.batch_id, i.sequence_index",
                    (folder_id,)):
                batch_id = db.row_text(row, "batch_id")
                if batch_id not in batch_costs:
                    batch_ids.append(batch_id)
                    batch_costs[batch_id] = row["split_cost_estimate"] or 0.0
                if row["bucket_key"]:
                    scan_keys.append(db.row_text(row, "bucket_key"))
            # Split proposals are billed to the batch and counted by get_total_cost, so they belong
            # in the same total as the drafts.
            cost = sum(batch_costs.values()) + sum(
                row["cost_estimate"] or 0.0 for row in document_rows)
        return {"folder_id": folder_id, "document_ids": document_ids, "batch_ids": batch_ids,
                "scan_keys": scan_keys, "cost": cost}

    def _purge_replaced_folder(request: Request, before: dict) -> dict:
        """Removes the folder the replacement upload supersedes -- documents, scans and everything
        the team did to them."""
        folder_id, document_ids = before["folder_id"], before["document_ids"]
        if not document_ids and not before["batch_ids"]:
            return {"documents": 0, "purged": {}}       # nothing was there to replace
        with db.connect(settings.db_path) as conn:
            folder_display_id = db.folder_display(conn, folder_id)[0]
        reason = f"replaced by a new upload of {folder_display_id}"
        reviewer = _reviewer_name(request) or ""
        with db.connect(settings.db_path) as conn:
            purged = db.purge_folder_state(conn, folder_id, "new", document_ids)
            # What these documents cost stays on the books; nothing was refunded by discarding them.
            db.record_deletion(
                conn, reviewer=reviewer, reason=reason, workflow="new",
                target_kind="new_folder", target_id=folder_id,
                summary=(f"{folder_display_id} replaced by a new upload ({len(document_ids)} "
                         f"documents, {len(before['scan_keys'])} scans)"),
                cost_estimate=before["cost"],
                # The folder lives on under the same id, so these lists are what say which
                # documents and batches went with the old one (see backup.apply_deletions).
                details={"document_ids": document_ids, "batch_ids": before["batch_ids"],
                         "purged": purged, "replaced": True},
            )
            for document_id in document_ids:
                db.delete_new_document(conn, document_id)
            for batch_id in before["batch_ids"]:
                db.delete_batch_rows(conn, batch_id)
        storage.delete_keys(before["scan_keys"])
        for batch_id in before["batch_ids"]:
            storage.delete_prefix(storage_mod.UPLOADS_PREFIX, batch_id)
        # The old folder's deliverable describes documents that no longer exist.
        export_archive.forget(export_dir, folder_id)
        for document_id in document_ids:
            storage.delete_document(folder_id, document_id)
            storage.delete_record(folder_id, document_id)
        return {"documents": len(document_ids), "purged": purged}

    @app.post("/api/new/batches")
    def post_new_batch(
        request: Request,
        files: list[UploadFile] = File(...), folder_display_id: str = Form(""),
        folder_name: str = Form(""), replace: bool = Form(False), tab_id: int = Form(db.DEFAULT_TAB_ID),
    ):
        """Uploads a folder of bulk-scanned images in the order the browser sent them.
        `folder_display_id` is the folder's id as people know it ("E2002"); the server finds or
        creates the folder it names."""
        display_id = folder_display_id.strip()
        with db.connect(settings.db_path) as conn:
            _require_tab_open(conn, tab_id)
        if len(files) > MAX_UPLOAD_FILES:
            raise HTTPException(
                status_code=413,
                detail=f"{len(files)} files in one upload exceeds the {MAX_UPLOAD_FILES}-scan limit — "
                       "split the folder and upload it in parts.",
            )
        if display_id:
            try:
                docid.require_safe_id(display_id, "Folder ID")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Before replacement cleanup or the first bucket write: an oversized request is rejected
        # atomically and cannot leave a half-ingested directory behind.
        _validate_upload_sizes(files)
        # Uploading a folder that already exists used to just happen, and the new scans landed
        # beside the old ones under a folder that was still assigned, still approved, still marked
        # exported.
        # One upload at a time per folder. Two overlapping replacements would each snapshot the
        # folder before the other purged it, and both replacement batches would survive.
        with _folder_upload_lock(display_id):
            viewer = _viewer(request)
            is_admin = scoping.is_admin(viewer)
            replacing = None
            if display_id:
                with db.connect(settings.db_path) as conn:
                    _refuse_another_spelling(conn, display_id, tab_id)
                    found = db.find_folder(conn, display_id, tab_id)
                    existing = (db.folder_has_work(conn, found["folder_id"], "new") if found is not None
                                else {"documents": 0})
                if existing["documents"]:
                    if not is_admin:
                        raise HTTPException(
                            status_code=409,
                            detail=f"{display_id} already exists in the archive. Ask an editor to "
                                   f"replace it — re-uploading a folder discards the work already "
                                   f"done on it, so a reviewer cannot do this.",
                        )
                    if not replace:
                        raise HTTPException(
                            status_code=409,
                            detail=_replacement_warning(existing),
                            headers={"X-Folder-Exists": display_id},
                        )
                # Whatever the folder holds, not only its documents: a folder replaced once and
                # then replaced again has an unsplit batch and no documents yet, and leaving that
                # behind is how two replacements both end up live.
                # Taken, not acted on: a folder is only discarded once its replacement has been
                # decoded and stored, so an unreadable scan leaves the existing folder untouched.
                if replace and is_admin and found is not None:
                    replacing = _folder_before_replacement(found["folder_id"])
                with db.connect(settings.db_path) as conn:
                    _require_folder_available_for_upload(conn, request, display_id, tab_id)
            # _LazyUploads, not a list comprehension: the bytes are read one image at a time as
            # run_ingest_batch consumes them, so a large folder's total size never determines peak
            # memory.
            uploads = _LazyUploads(files)
            try:
                # Attributed to the uploader so the banner is theirs alone -- a reviewer watching their
                # own folder arrive should not also be watching four other people's.
                uploader = _viewer(request)
                with activity.track(
                    "uploading", f"Storing {len(files)} uploaded scan(s)",
                    owner_id=uploader["id"] if uploader else None,
                    folder=folder_name or display_id, remaining=len(files),
                ):
                    batch_id = run_ingest_batch(
                        uploads, settings, folder_display_id=display_id, folder_name=folder_name,
                        uploaded_by=uploader["id"] if uploader else None, tab_id=tab_id,
                    )
            except ValueError as exc:
                detail = str(exc)
                # run_ingest_batch only sees what _LazyUploads handed it, so a folder of nothing but
                # PDFs/OS junk reaches it looking like an empty upload.
                if uploads.skipped and detail == "no files uploaded":
                    detail = (
                        f"none of the {len(files)} uploaded file(s) were scan images — "
                        f"skipped as non-image files: {', '.join(uploads.skipped[:5])}"
                        + (" …" if len(uploads.skipped) > 5 else "")
                    )
                raise HTTPException(status_code=400, detail=detail)

            # The swap: the new batch is already stored and recorded, so discarding the old folder now
            # cannot leave the archive with neither.
            if replacing is not None:
                _purge_replaced_folder(request, replacing)
            # Inside the lock: once it is released, the next replacement may purge this very batch.
            payload = _batch_payload(batch_id)

        return payload

    @app.get("/api/new/batches/{batch_id}")
    def get_new_batch(request: Request, batch_id: str):
        with db.connect(settings.db_path) as conn:
            _require_batch_access(conn, request, batch_id)
        return _batch_payload(batch_id)

    @app.get("/api/new/batches/{batch_id}/progress")
    def get_batch_progress(request: Request, batch_id: str):
        """Cheap, frequently-pollable progress check for a batch's split proposal -- how many pages
        the model hasn't examined yet (see split.run_propose_split's split_proposal_cursor), not the
        full batch payload (page image URLs, suggested DocIDs, ...) get_new_batch builds on
        every call."""
        with db.connect(settings.db_path) as conn:
            batch = _require_batch_access(conn, request, batch_id)
        pending = max(batch["image_count"] - (batch["split_proposal_cursor"] or 0), 0)
        return {"pending": pending}

    @app.get("/api/new/batches/{batch_id}/images/{sequence_index}")
    def get_new_batch_image(request: Request, batch_id: str, sequence_index: int,
                            size: str = imaging.DEFAULT_DISPLAY_SIZE):
        # One row, not the whole batch.
        with db.connect(settings.db_path) as conn:
            _require_batch_access(conn, request, batch_id)
            image = db.get_batch_image(conn, batch_id, sequence_index)
        if image is None or not image["bucket_key"]:
            raise HTTPException(status_code=404, detail="image not found")
        return _serve_page_image(request, image["bucket_key"], size)

    @app.post("/api/new/suggest-doc-ids")
    def post_suggest_more_doc_ids(request: Request, payload: SuggestMoreDocIdsPayload):
        """Called from the split-review screen whenever the reviewer's own toggling creates more
        document groups than the initial batch response suggested names for -- e.g. the model missed
        a boundary and the reviewer adds it by hand, producing a new trailing group with no name
        yet."""
        with db.connect(settings.db_path) as conn:
            _require_folder_access(conn, request, payload.folder_id, "new")
        ids = suggest_more_document_ids(payload.folder_id, payload.count, set(payload.exclude), settings)
        return {"suggested_document_ids": ids}

    @app.post("/api/new/batches/{batch_id}/confirm")
    def post_confirm_split(request: Request, batch_id: str, payload: ConfirmSplitPayload):
        """The one step automation doesn't drive: the reviewer's finalized page groupings and
        document names."""
        with db.connect(settings.db_path) as conn:
            batch = _require_batch_access(conn, request, batch_id)
            _require_tab_open(conn, batch["tab_id"])
        try:
            # Exclusive against drafting this folder, which reads the documents a confirm replaces.
            with activity.exclusive(
                "confirming", f"Creating {len(payload.groups)} document(s) from the confirmed split",
                folder=_batch_folder(batch_id), remaining=len(payload.groups),
                key=_draft_key("new", db.row_text(batch, "folder_id")), blocks=("drafting",),
            ):
                created_ids = confirm_batch_split(batch_id, [g.model_dump() for g in payload.groups], settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        with db.connect(settings.db_path) as conn:
            app_settings = _sync_app_settings(conn, settings)
            created_folder = _folder_of_ids(conn, created_ids)
        terminal = ""
        try:
            with activity.exclusive("drafting", _drafting_label(settings, "new documents"),
                                    folder=_batch_folder(batch_id),
                                    remaining=len(created_ids),
                                    key=_draft_key("new", created_folder)) as job:
                # This split's own documents: the global pending queue holds other people's work,
                # and confirming a folder must not spend the budget drafting it.
                finished = _draft_job(request, created_ids[:app_settings["new_document_batch_size"]],
                                      created_folder, progress=_counts_down(job))
            # The split is already durable: an account problem is reported, not turned into a 500.
            terminal = _terminal(finished)
        except openrouter.AccountError as exc:
            terminal = str(exc)
        # transcribe_new_document isolates per-document failures (never raises out of
        # run_transcribe_new_documents) -- without this, a batch that fails on every document (e.g.
        # OpenRouter out of credits) reports created_ids with no indication drafting them all just
        # failed; the reviewer would only find out by noticing the queue never gains any documents.
        with db.connect(settings.db_path) as conn:
            error = db.get_new_document_error(conn, created_ids)
        return {"created": created_ids, "error": error or terminal or None}

    @app.get("/api/new/merge")
    def get_merge_preview(request: Request, document_ids: str):
        """What merging the given documents would do, without doing it: the target DocID, the page
        count, and the renumbering that will follow."""
        ids = [d.strip() for d in document_ids.split(",") if d.strip()]
        with db.connect(settings.db_path) as conn:
            _require_documents_access(conn, request, ids, "new")
        try:
            plan = plan_merge(ids, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return plan

    @app.post("/api/new/merge")
    def post_merge(request: Request, payload: MergePayload):
        """Rejoins wrongly-split documents into one."""
        # Merging spends money, so it honours the cap the reviewer set in the Settings menu, not
        # the value this process happened to start with.
        with db.connect(settings.db_path) as conn:
            _require_documents_access(conn, request, payload.document_ids, "new")
            for document_id in payload.document_ids:
                _require_folder_open(conn, request, document_id, "new")
            _sync_app_settings(conn, settings)
            merge_folder = _folder_of_ids(conn, payload.document_ids)
        try:
            # Keyed on the folder, so a draft of it cannot run while its documents are rebuilt.
            with activity.exclusive(
                "confirming", f"Merging {len(payload.document_ids)} documents into one",
                key=_draft_key("new", merge_folder), blocks=("drafting",),
            ):
                return merge_documents(payload.document_ids, settings,
                                       author_id=_owner_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            # The all-time spend cap -- a refusal to start, not a failure partway through, so the
            # folder is untouched and the reviewer can raise the cap and retry.
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/new/resplit")
    def get_resplit(request: Request, document_ids: str):
        """Seeds the "Fix Split" screen for a comma-separated set of already-confirmed documents:
        their original intake pages pooled in scan order, tagged with the grouping they have
        now."""
        ids = [d.strip() for d in document_ids.split(",") if d.strip()]
        with db.connect(settings.db_path) as conn:
            _require_documents_access(conn, request, ids, "new")
        try:
            payload = collect_resplit_pages(ids, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Suggested names for however many documents the reviewer ends up making, so the screen can
        # pre-fill a new group's id without a second round trip: the current names first.
        current = payload["display_ids"]
        with db.connect(settings.db_path) as conn:
            existing = db.display_ids_in_folder(conn, payload["folder_id"])
            scheme = db.folder_id_scheme(conn, payload["folder_id"])
        spare = docid.next_document_ids(payload["folder_display_id"], existing - set(current),
                                        len(ids) + 4, scheme)
        payload["suggested_document_ids"] = current + [s for s in spare if s not in current]
        return payload

    @app.post("/api/new/resplit")
    def post_resplit(request: Request, payload: ResplitPayload):
        """Applies a corrected split across already-drafted documents and immediately re-drafts
        what comes back."""
        with db.connect(settings.db_path) as conn:
            _require_documents_access(conn, request, payload.document_ids, "new")
            for document_id in payload.document_ids:
                _require_folder_open(conn, request, document_id, "new")
            folder_id = _folder_of_ids(conn, payload.document_ids)
        try:
            with activity.exclusive("confirming", f"Rebuilding {len(payload.document_ids)} document(s) with new boundaries",
                                    key=_draft_key("new", folder_id),
                                    blocks=("drafting",)):
                created_ids = resplit_documents(payload.document_ids, [g.model_dump() for g in payload.groups], settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        with db.connect(settings.db_path) as conn:
            app_settings = _sync_app_settings(conn, settings)
        with activity.exclusive("drafting", _drafting_label(settings, "new documents"),
                                folder=_new_folder_label(folder_id),
                                key=_draft_key("new", folder_id)):
            run_transcribe_new_documents(
                settings,
                NewDocumentOptions(limit=app_settings["new_document_batch_size"], max_total_cost=settings.max_total_cost),
                # Same reason as post_confirm_split: only the documents this resplit just made.
                document_ids=created_ids,
            )
        # Both boundary-fixing paths leave the folder's display-id sequence dense.
        renumbered = renumber_folder(folder_id, settings)

        with db.connect(settings.db_path) as conn:
            error = db.get_new_document_error(conn, created_ids)
        return {
            "created": created_ids,
            "replaced": payload.document_ids,
            "renumbered": renumbered,
            "error": error,
        }

    @app.get("/api/new/pending-count")
    def get_pending_count(request: Request, folder_id: str | None = None, tab_id: int | None = None):
        """How many new-document groups are still stuck at 'split_confirmed' or 'error' -- meant to
        be polled by the frontend *while* a confirm/draft-pending request is in flight (FastAPI
        runs sync routes like this one in a thread pool, so it's served concurrently even though"""
        with db.connect(settings.db_path) as conn:
            if folder_id is not None:
                _require_folder_access(conn, request, folder_id, "new")
            ids = (
                db.get_new_pending_document_ids_in_folder(conn, folder_id)
                if folder_id is not None
                else db.get_new_pending_document_ids(conn, tab_id)
            )
            visible = scoping.visible_folder_ids(conn, _viewer(request), "new")
            if visible is not None:
                ids = [document_id for document_id in ids
                       if db.folder_of_document(conn, document_id) in visible]
            return {"pending": len(ids)}

    @app.post("/api/new/draft-pending")
    def post_draft_pending(request: Request):
        """Manually retries drafting up to the Settings menu's configured transcribe batch size
        worth of new-document group(s) still stuck at 'split_confirmed' or 'error' -- e.g.
        because the shared $ cap was hit right after confirm, a transient OpenRouter failure, or
        confirm's own auto-draft left some of a large batch undrafted by design (see
        post_confirm_split)."""
        with db.connect(settings.db_path) as conn:
            pending_before = db.get_new_pending_document_ids(conn)
            # A reviewer may spend the shared budget -- finishing the folder they were given matters
            # more than the cost -- but only on that folder.
            visible = scoping.visible_folder_ids(conn, _viewer(request), "new")
            if visible is not None:
                pending_before = [
                    doc_id for doc_id in pending_before
                    if db.folder_of_document(conn, doc_id) in visible
                ]
            app_settings = _sync_app_settings(conn, settings)
        attempted_ids = pending_before[: app_settings["new_document_batch_size"]]
        terminal = ""
        try:
            with activity.exclusive("drafting", _drafting_label(settings, "new documents")):
                terminal = _terminal(_draft_job(request, attempted_ids, None))
        except openrouter.AccountError as exc:
            terminal = str(exc)
        with db.connect(settings.db_path) as conn:
            # Counted off the ids this call attempted, not off the size of the global queue: a
            # reviewer's own documents can all be drafted while somebody else's arrive behind them,
            # and that reported as nothing drafted.
            still_pending = set(db.get_new_pending_document_ids(conn))
            drafted = [doc_id for doc_id in attempted_ids if doc_id not in still_pending]
            # transcribe_new_document isolates per-document failures (never raises out of
            # run_transcribe_new_documents) -- without this, a batch that fails on every document
            # (e.g. OpenRouter out of credits) reports created_ids with no indication drafting them
            # all just failed; the reviewer would only find out by noticing the queue never gains
            # any documents.
            error = db.get_new_document_error(conn, attempted_ids)
        return {"drafted": len(drafted), "error": error or terminal or None}

    @app.post("/api/new/folders/draft")
    def post_new_folder_draft(request: Request, folder_id: str, count: int | None = None,
                              total: int | None = None, done: int = 0):
        """Drafts undrafted ('split_confirmed' or 'error') documents from a single archival folder,
        in DocID order -- lets a reviewer pace drafting per-folder rather than only through the
        workflow-wide Draft Pending button and its shared batch size."""
        if count is not None and count < 1:
            raise HTTPException(status_code=400, detail="count must be at least 1")
        with db.connect(settings.db_path) as conn:
            _require_folder_access(conn, request, folder_id, "new")
            _require_folder_tab_open(conn, folder_id)
            pending_ids = db.get_new_pending_document_ids_in_folder(conn, folder_id)
            if not pending_ids:
                raise HTTPException(status_code=404, detail="folder not found or has no documents awaiting drafting")
            _sync_app_settings(conn, settings)

        requested = pending_ids if count is None else pending_ids[:count]
        target_ids = requested[:MAX_DOCUMENTS_PER_DRAFT_REQUEST]

        # The client sends a long run in chunks, so it says how many the reviewer asked for and how
        # many are already done; the bar then names the whole run rather than this slice of it.
        asked_for = total if total and total > 0 else len(target_ids)
        left = max(asked_for - max(done, 0), len(target_ids))
        terminal = ""
        try:
            with activity.exclusive(
                "drafting", _drafting_label(settings, f"{asked_for} document(s) in {_new_folder_label(folder_id)}"),
                folder=_new_folder_label(folder_id), remaining=left,
                key=_draft_key("new", folder_id),
            ) as job:
                # The one failure worth stopping for is the key or the balance, not this folder.
                terminal = _terminal(_draft_job(request, target_ids, folder_id,
                                                progress=_counts_down(job, left - len(target_ids))))
        except openrouter.AccountError as exc:
            terminal = str(exc)

        with db.connect(settings.db_path) as conn:
            remaining_ids = db.get_new_pending_document_ids_in_folder(conn, folder_id)
            still_pending = set(remaining_ids)
            drafted = [doc_id for doc_id in target_ids if doc_id not in still_pending]
            # Same "tell a working-but-slow draft apart from everything silently failing" signal
            # post_draft_pending gives -- see its own comment for why this matters.
            error = db.get_new_document_error(conn, target_ids)
        return {
            "drafted": len(drafted),
            "attempted": len(target_ids),
            "remaining": len(remaining_ids),
            "error": error,
            "terminal": terminal,
        }

    @app.get("/api/new/documents")
    def list_new_documents(request: Request, tab_id: int | None = None):
        with db.connect(settings.db_path) as conn:
            rows = db.get_new_review_queue(conn, tab_id)
        role = _viewer_role(request)
        with db.connect(settings.db_path) as conn:
            mine = db.get_document_reviews(conn, "new", role)
            reviewers = db.get_role_verdicts(conn, "new", "reviewer") if role == "admin" else {}
            # Which of them the admin picked out when they sent the folder back.
            sent_back = (set() if role == "admin"
                         else collab.documents_sent_back(conn, None, "new"))
            comments = collab.comment_counts(conn, "new")
        listed = [
            {
                "document_id": row["document_id"],
                "display_id": row["display_id"] or "",
                "folder_id": row["folder_id"] or "",
                "folder_display_id": row["folder_display_id"] or "",
                "title": _blank(row["title"]),
                "page_count": row["page_count"],
                "review_status": (row["review_status"] if role == "admin"
                                  else mine.get(row["document_id"], "unreviewed")),
                "sent_back": row["document_id"] in sent_back,
                "challenges": json.loads(row["challenges_json"] or "[]"),
                **_comment_counts(comments, row["document_id"]),
                **_verdicts(row, role, reviewers),
            }
            for row in rows
        ]
        # Narrowed to the folders this viewer holds.
        with db.connect(settings.db_path) as conn:
            return scoping.filter_documents(conn, _viewer(request), listed, "new")

    @app.get("/api/new/folders")
    def list_new_folders(request: Request, tab_id: int | None = None):
        """The "Add New Document" workflow's documents grouped into their archival folders (by
        DocID prefix) for the folder-browser UI -- each with a document count, accepted count,
        and the all_approved flag the folder-scoped Export gate keys on."""
        with db.connect(settings.db_path) as conn:
            folders = scoping.filter_folders(
                conn, _viewer(request), db.get_new_document_folders(conn, tab_id), "new")
            folders = _with_export_state(conn, folders, "new")
            folders = _with_reviewers(conn, folders, "new")
            return scoping.strip_admin_fields(folders, _viewer(request))

    @app.delete("/api/new/folders")
    def delete_new_folder(request: Request, folder_id: str, reason: str = ""):
        """Permanently removes every document in one archival folder -- their DB rows and bucket
        images."""
        reviewer, reason = _deletion_context(request, reason)
        with db.connect(settings.db_path) as conn:
            document_ids = db.get_new_document_ids_in_folder(conn, folder_id)
            if not document_ids:
                raise HTTPException(status_code=404, detail="folder not found or already empty")
            folder_display_id = db.folder_display(conn, folder_id)[0]
            batch_ids = [row["batch_id"] for row in conn.execute(
                "SELECT batch_id FROM new_batches WHERE folder_id = ?", (folder_id,))]
            folder_cost = sum(db.get_new_document_recorded_cost(conn, d) for d in document_ids)
            # The folder's documents are only half of it.
            purged = db.purge_folder_state(conn, folder_id, "new", document_ids)
            db.record_deletion(
                conn,
                reviewer=reviewer,
                reason=reason,
                workflow="new",
                target_kind="new_folder",
                target_id=folder_id,
                summary=f"{folder_display_id or 'Ungrouped'} ({len(document_ids)} documents)",
                cost_estimate=folder_cost,
                details={"document_ids": document_ids, "batch_ids": batch_ids,
                         "folder_display_id": folder_display_id, "purged": purged},
            )
            for document_id in document_ids:
                db.delete_new_document(conn, document_id)
        # Everything the folder put in the bucket, not just its documents.
        removed_uploads = _delete_folder_uploads(folder_id)
        with db.connect(settings.db_path) as conn:
            db.delete_batches_in_folder(conn, folder_id)
            conn.execute("DELETE FROM folders WHERE folder_id = ?", (folder_id,))
        storage.delete_prefix(storage_mod.DOCUMENTS_PREFIX, folder_id)
        storage.delete_prefix(storage_mod.RECORDS_PREFIX, folder_id)
        # The export tree is authoritative for the backups, so a deliverable left here would go on
        # being replicated to both of them after the folder itself is gone.
        export_archive.forget(export_dir, folder_id)
        return {"ok": True, "deleted_count": len(document_ids),
                "upload_folders_removed": removed_uploads}

    @app.get("/api/reviewer-colours")
    def get_reviewer_colours():
        """Every reviewer colour, and who holds each."""
        with db.connect(settings.db_path) as conn:
            held = accounts.reviewer_colours(conn)
            names = {row["id"]: row["display_name"] for row in
                     conn.execute("SELECT id, display_name FROM users")}
        holder = {colour: user_id for user_id, colour in held.items()}
        return [{"key": key, "bg": bg, "ink": ink, "held_by": holder.get(key),
                 "held_by_name": names.get(holder.get(key), "")}
                for key, bg, ink in accounts.REVIEWER_COLOURS]

    @app.post("/api/reviewer-colours/mine")
    def set_my_reviewer_colour(request: Request, payload: ColourPayload):
        viewer = _viewer(request)
        if viewer is None:
            raise HTTPException(status_code=401, detail="sign in to choose a colour")
        with db.connect(settings.db_path) as conn:
            try:
                accounts.set_colour(conn, viewer["id"], payload.colour)
            except accounts.PermissionDenied as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            except accounts.AuthError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "colour": payload.colour}

    @app.post("/api/new/documents/{document_id}/redraft")
    def post_redraft_document(
        request: Request,
        document_id: str,
        _write_lock: None = Depends(_lock_new_document_redraft),
    ):
        """Drafts one document again, for the times the model returns nothing for it."""
        with db.connect(settings.db_path) as conn:
            if db.get_new_document(conn, document_id) is None:
                raise HTTPException(status_code=404, detail="document not found")
            # A reviewer may spend on the folder they were given, and on nothing else -- the same
            # rule Draft Pending follows.
            visible = scoping.visible_folder_ids(conn, _viewer(request), "new")
            folder_id = db.folder_of_document(conn, document_id)
            if visible is not None and folder_id not in visible:
                raise HTTPException(status_code=403, detail="that document is not yours to draft")
            _require_folder_open(conn, request, document_id, "new")
            app_settings = _sync_app_settings(conn, settings)
            spent = db.get_total_cost(conn)
            before = db.get_new_document(conn, document_id)
        if spent >= app_settings["max_total_cost"]:
            raise HTTPException(
                status_code=409,
                detail=f"the ${app_settings['max_total_cost']:.2f} spend cap has been reached "
                       f"(${spent:.4f} used). Raise it in Settings to draft again.")

        try:
            with activity.exclusive("drafting", _drafting_label(settings, db.row_text(before, "display_id")),
                                    folder=_new_folder_label(folder_id),
                                    key=_draft_key("new", folder_id)):
                run_transcribe_new_documents(
                    settings,
                    NewDocumentOptions(limit=1, max_total_cost=app_settings["max_total_cost"]),
                    document_ids=[document_id],
                )
        except openrouter.AccountError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        with db.connect(settings.db_path) as conn:
            row = db.get_new_document(conn, document_id)
            drafted = (db.row_text(row, "status") == "drafted"
                       and db.row_text(row, "run_date") != db.row_text(before, "run_date"))
            if drafted:
                _show_the_new_draft(conn, request, before, row)
                return {"ok": True, "status": "drafted", "drafted": True,
                        "characters": len(db.row_text(row, "text") or "")}
            error = db.row_text(row, "error_message")
            if db.row_text(before, "status") == "drafted":
                # A failed redraft keeps the draft it was meant to replace: flipping to 'error' took
                # an accepted document out of the export and back into Draft Pending.
                conn.execute("UPDATE new_documents SET status = 'drafted', error_message = NULL "
                             "WHERE document_id = ?", (document_id,))
        return {"ok": True, "status": db.row_text(before, "status") or "error", "drafted": False,
                "characters": 0, "error": error or "the model did not return a new draft"}

    def _show_the_new_draft(conn, request, before, after) -> None:
        """Clears the reviewer's copies a redraft replaces -- the text and the page fields, which are
        read off the page -- recording each replaced value in History; document fields are kept."""
        registry = profiles.registry_for(conn, after)
        page_keys = [f["key"] for f in registry if f["scope"] == "page"]
        before_values, after_drafted = fields.effective(before, registry), fields.drafted(after)
        viewer = _viewer(request)
        db.record_text_revisions(
            conn, after["document_id"], "new",
            {"reviewed_text": after["text"] or "",
             **{collab.FIELD_PREFIX + k: fields.clean(fields.by_key(registry)[k], after_drafted.get(k))
                for k in page_keys}},
            viewer["id"] if viewer else None,
            previous_values={"reviewed_text": db.effective_text(before),
                             **{collab.FIELD_PREFIX + k: before_values.get(k) for k in page_keys}},
            coalesce=False, labels=_field_labels(registry),
        )
        # After its rows, so no later edit extends them into a sitting of its own.
        collab.record_redraft_note(conn, after["document_id"], "new",
                                   profiles.label(conn, profiles.document_version_id(conn, after)),
                                   viewer["id"] if viewer else None)
        kept = {k: v for k, v in fields.reviewed(after).items() if k not in page_keys}
        conn.execute("UPDATE new_documents SET reviewed_text = NULL, reviewed_fields_json = ? "
                     "WHERE document_id = ?", (json.dumps(kept, ensure_ascii=False), after["document_id"]))
        refresh_output_record(conn, after["document_id"], storage)

    def _field_labels(registry) -> dict:
        return {collab.FIELD_PREFIX + f["key"]: f["label"] for f in registry}

    @app.get("/api/new/documents/{document_id}")
    def get_new_document_detail(request: Request, document_id: str):
        with db.connect(settings.db_path) as conn:
            _require_document_access(conn, request, document_id, "new")
            row = db.get_new_document_for_review(conn, document_id)
            if row is None:
                raise HTTPException(status_code=404, detail="document not found")
            pages = db.get_new_document_pages(conn, document_id)
            orientations = db.page_orientations(conn, "new", document_id)
            locked = _locked_for(conn, request, document_id, "new")

            registry, values = db.field_values(conn, row)
            title = row["title_override"] or title_for_row(conn, row, values)
            folder_display_id = db.folder_display(conn, row["folder_id"])[0]
            version_id = profiles.document_version_id(conn, row)
            version_label = profiles.label(conn, version_id)

        return {
            "document_id": row["document_id"],
            "display_id": row["display_id"] or "",
            "folder_id": row["folder_id"] or "",
            "folder_display_id": folder_display_id,
            "locked": locked,
            "draft_version": db.row_text(row, "run_date"),
            "metadata": {
                "title": title,
                "title_is_override": bool(row["title_override"]),
            },
            # The form is built from the registry of the version that drafted this document.
            "fields": values,
            "field_registry": fields.describe(registry),
            "transcription": {
                "text": db.effective_text(row),
                "uncertain_words": json.loads(row["uncertain_words_json"] or "[]"),
            },
            "review": {
                "status": _own_status(row, request, "new"),
                "reviewer": _blank(row["reviewer"]),
                # Who reviewed it, for the line the document carries at the bottom.
                "reviewed_by": (db.signed_reviewer(row) if _viewer_role(request) == "admin" else ""),
                "editors_notes": _blank(row["editors_notes"]),
                "reviewed_at": row["reviewed_at"],
            },
            "pipeline_info": {
                "model": row["model"],
                "prompt_version": version_label,
                "prompt_version_id": version_id,
                "run_date": row["run_date"],
                # What a draft cost is the admin's business; a reviewer is shown the model and
                # prompt behind their document, not the spend.
                "cost_estimate": (row["cost_estimate"] if _viewer_role(request) == "admin"
                                  else None),
            },
            "pages": [
                {
                    "sequence_index": p["sequence_index"],
                    "width": p["width"],
                    "height": p["height"],
                    "image_url": f"/api/new/documents/{document_id}/pages/{p['sequence_index']}/image",
                    **orientations.get(p["sequence_index"], {"rotation": 0, "mirrored": False}),
                }
                for p in pages
                if p["bucket_key"]
            ],
        }

    @app.get("/api/new/documents/{document_id}/pages/{sequence_index}/image")
    def get_new_document_page_image(request: Request, document_id: str, sequence_index: int,
                                    size: str = imaging.DEFAULT_DISPLAY_SIZE):
        with db.connect(settings.db_path) as conn:
            _require_document_access(conn, request, document_id, "new")
            pages = db.get_new_document_pages(conn, document_id)
        page = next((p for p in pages if p["sequence_index"] == sequence_index), None)
        if page is None or not page["bucket_key"]:
            raise HTTPException(status_code=404, detail="page image not found")
        return _serve_page_image(request, page["bucket_key"], size)

    @app.post("/api/new/documents/{document_id}/review")
    def post_new_review(
        request: Request,
        document_id: str,
        payload: NewReviewPayload,
        _write_lock: None = Depends(_lock_new_document_save),
    ):
        if payload.review_status not in (*REVIEW_STATUSES, collab.NEEDS_WORK):
            raise HTTPException(status_code=400, detail=f"review_status must be one of {REVIEW_STATUSES}")

        with db.connect(settings.db_path) as conn:
            # See the existing-document review route: role ownership depends on reading the latest
            # shared status and writing the edit as one transaction.
            conn.execute("BEGIN IMMEDIATE")
            _require_document_access(conn, request, document_id, "new")
            existing = db.get_new_document_for_review(conn, document_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="document not found")
            if (payload.draft_version is not None
                    and payload.draft_version != db.row_text(existing, "run_date")):
                raise HTTPException(
                    status_code=409,
                    detail="this document was redrafted after you opened it; reopen it before saving",
                )
            registry, prior_values = db.field_values(conn, existing)
            try:
                given = fields.validate_review(registry, payload.fields)
            except fields.PayloadError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _require_folder_open(conn, request, document_id, "new")
            _require_status_allowed(conn, request, document_id, "new", payload.review_status)
            # A field the form did not send keeps what the reviewer saved before.
            reviewed_fields = {**fields.reviewed(existing), **given}
            values = {**prior_values, **given}
            # Cached so the queue (plain SQL) can show the derived title without recomputing it.
            reviewed_title = title_for_row(conn, existing, values)
            typed_title = payload.title.strip()
            title_override = typed_title if typed_title and typed_title != reviewed_title else None
            prior_title = existing["title_override"] or title_for_row(conn, existing, prior_values)
            viewer = _viewer(request)
            db.record_text_revisions(
                conn, document_id, "new",
                {"reviewed_text": payload.reviewed_text, "reviewed_title": title_override or reviewed_title,
                 **{collab.FIELD_PREFIX + k: v for k, v in given.items()},
                 "editors_notes": payload.editors_notes},
                viewer["id"] if viewer else None,
                previous_values={"reviewed_text": db.effective_text(existing), "reviewed_title": prior_title,
                                 **{collab.FIELD_PREFIX + k: prior_values.get(k) for k in given},
                                 "editors_notes": existing["editors_notes"] or ""},
                labels=_field_labels(registry),
            )
            was_ready = folder_exports.ready_for(conn, "new", document_id)
            db.save_new_review(
                conn,
                document_id,
                reviewed_title=reviewed_title,
                title_override=title_override,
                reviewed_text=payload.reviewed_text,
                reviewed_fields=reviewed_fields,
                review_status=_status_to_store(conn, request, document_id, "new",
                                               payload.review_status),
                reviewer=_reviewer_to_store(conn, request, document_id, "new",
                                            payload.review_status),
                editors_notes=payload.editors_notes,
            )
            # The reviewer's own verdict, kept apart from the admin's (see _status_to_store).
            _record_role_review(conn, request, document_id, "new", payload.review_status,
                                _reviewer_name(request))
            # The stored record is what the backup copies, so it has to carry the edit that was
            # just made -- otherwise `new/<folder>/<id>/` keeps the AI draft for ever.
            refresh_output_record(conn, document_id, storage)
            # Approval follows the documents: granted when the last one is accepted, withdrawn
            # when one stops being.
            viewer = _viewer(request)
            collab.settle_approval(conn, document_id, "new", viewer["id"] if viewer else None)
            completed_folder = folder_exports.completion(conn, "new", document_id, was_ready)
        return {"ok": True, "completed_folder": completed_folder}

    @app.delete("/api/new/documents/{document_id}")
    def delete_new_document_endpoint(request: Request, document_id: str, reason: str = ""):
        """Permanently removes a document from the New Documents workflow: its DB rows
        (new_document_pages, new_documents) and its bucket page images."""
        reviewer, reason = _deletion_context(request, reason)
        with db.connect(settings.db_path) as conn:
            row = db.get_new_document(conn, document_id)
            if row is None:
                raise HTTPException(status_code=404, detail="document not found")
            db.record_deletion(
                conn,
                reviewer=reviewer,
                reason=reason,
                workflow="new",
                target_kind="new_document",
                target_id=document_id,
                summary=row["title_override"] or row["ai_title"] or row["display_id"] or document_id,
                cost_estimate=db.get_new_document_recorded_cost(conn, document_id),
                details={"status": row["status"], "batch_id": row["batch_id"],
                         "display_id": row["display_id"], "folder_id": row["folder_id"]},
            )
            folder_id = row["folder_id"]
            db.delete_new_document(conn, document_id)
        storage.delete_document(folder_id, document_id)
        storage.delete_record(folder_id, document_id)
        # The folder's display ids close up behind the deleted one; no file moves.
        return {"ok": True, "renumbered": renumber_folder(folder_id, settings)}

    @app.post("/api/new/documents/{document_id}/rename")
    def post_new_document_rename(request: Request, document_id: str, payload: RenameDocumentPayload):
        """Gives a document a new display id -- a typo, or a suggestion that turned out wrong. Its
        internal id, files, comments, history and verdicts are untouched."""
        new_display_id = payload.new_display_id.strip()
        if not new_display_id:
            raise HTTPException(status_code=400, detail="new document id cannot be blank")
        # A display id ends up in export paths and file names -- keep it a safe token.
        try:
            docid.require_safe_id(new_display_id, "document id")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        with db.connect(settings.db_path) as conn:
            _require_document_access(conn, request, document_id, "new")
            # Any status, not only drafted: a rename should work wherever the document is.
            current = db.get_new_document(conn, document_id)
            if current is None:
                raise HTTPException(status_code=404, detail="document not found")
            _require_folder_open(conn, request, document_id, "new")
            try:
                db.rename_new_document(conn, document_id, new_display_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # The derived title caches open with the display id; a typed override stays verbatim.
            retitle(conn, document_id, storage)
        return {"document_id": document_id, "display_id": new_display_id}

    profile_routes.install(app, settings)
    tab_routes.install(app, settings)
    access_routes.install(app, settings)
    model_routes.install(app, settings, job_handlers)
    backup_routes.install(app, settings)
    update_routes.install(app, settings)
    app.state.job_handlers = job_handlers
    # The owner's saved service and key, when there is one; a checkout's environment key otherwise.
    with db.connect(settings.db_path) as conn:
        if backends.get(conn)["api_key_enc"]:
            backends.apply(conn, settings)

    # Authentication and the single authorization gate.
    from . import webauth
    webauth.install(app, settings)

    if APP_STATIC_DIR.exists():
        # Mounted last so /login and the account routes are matched before this catch-all.
        app.mount("/", NoCacheStaticFiles(directory=str(APP_STATIC_DIR), html=True), name="static")

    return app


def run_server(
    settings: Settings,
    export_dir: str = "data/exports",
    host: str = "127.0.0.1",
    port: int = 8000,
    backup_dir: str | None = None,
    backup_mirror: str | None = None,
    backup_status_dir: str = "deploy/logs",
    backup_interval_minutes: float = backup.BACKUP_INTERVAL_MINUTES,
    snapshot_interval_minutes: float = backup.SNAPSHOT_INTERVAL_MINUTES,
) -> bool:
    """Serves the review UI, and -- when `backup_dir` is set -- backs the database up while it
    does. Returns True when the site stopped to be started again (a data-folder move)."""
    app = create_app(
        settings, export_dir, backup_status_dir,
        backup_dir=backup_dir, backup_mirror=backup_mirror,
    )
    timer = {"stop": None}

    def start_backups(destination, mirror):
        timer["stop"] = backup.start_backup_timer(
            settings.db_path, destination,
            data_dir=str(Path(settings.db_path).parent),
            status_dir=backup_status_dir, mirror_dir=mirror,
            interval_minutes=backup_interval_minutes,
            snapshot_interval_minutes=snapshot_interval_minutes,
            export_dir=export_dir,
        )

    def restart_backups():
        """A destination changed in Settings → Backups is used from the next backup on."""
        if timer["stop"] is not None:
            timer["stop"].set()
        folder = Path(settings.data_folder)
        # With the environment's overrides, as at start, so a saved change never moves backups off them.
        paths = datafolder.resolved_paths(folder, datafolder.load(folder))
        start_backups(str(paths["backups"]), str(paths["backup_mirror"]) if paths["backup_mirror"] else "")
    if settings.data_folder:
        app.state.restart_backups = restart_backups
    if backup_dir:
        start_backups(backup_dir, backup_mirror)
    else:
        logger.warning(
            "no backup destination configured -- set INCIPIT_BACKUP_DIR or pass --backup-dir, or "
            "this server writes to a database nothing is snapshotting")
    # Deletes close their own gaps, so this finds nothing almost every time.
    gaps.watch(settings)
    if settings.site_name and host not in ("127.0.0.1", "localhost", "::1"):
        # In the background: announcing takes a few seconds and must never hold up the site.
        threading.Thread(target=localname.announce, args=(settings.site_name, port), daemon=True,
                         name="incipit-local-name").start()
    servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port))]
    secure = _secure_listener(settings, host)
    if secure is not None:
        servers.append(uvicorn.Server(uvicorn.Config(app, host=host, port=settings.https_port,
                                                     ssl_certfile=str(secure[0]), ssl_keyfile=str(secure[1]))))
        # Only the encrypted port is opened to the internet, so no password crosses it in the clear.
        if settings.forward_port and host not in ("127.0.0.1", "localhost", "::1"):
            portmap.forward(settings.https_port)
    if settings.tunnel != "off" and settings.tunnel_dir:
        threading.Thread(target=access_routes.reach_outside, args=(settings,), daemon=True,
                         name="incipit-outside").start()
    asked = {"restart": False, "update": False}

    def request_restart():
        asked["restart"] = True
        servers[0].should_exit = True

    def stop_for_update():
        """The installer is replacing the program; the site stops and is not started again here."""
        asked["update"] = True
        servers[0].should_exit = True
    app.state.request_restart = request_restart
    app.state.stop_for_update = stop_for_update
    update_checks = threading.Event()
    threading.Thread(target=updates.check_forever, args=(update_checks,), daemon=True,
                     name="incipit-update-check").start()
    worker = jobs.Worker(settings.db_path, app.state.job_handlers).start()
    try:
        asyncio.run(_serve_together(servers))
    finally:
        update_checks.set()
        worker.stop()
        # Before anything else stops: a billed call still queued must reach the ledger.
        ledger.flush()
        localname.announce("", port)
        portmap.forward(None)
        access_routes.leave_outside(settings)
        tls.serving.update(port=0)
    return "update" if asked["update"] else asked["restart"]


def _secure_listener(settings: Settings, host: str):
    """The certificate and key for the HTTPS port, or None (logged and shown on Access) when it cannot open."""
    tls.serving.update(port=0, error="")
    if not settings.https_port or not settings.tls_dir:
        return None
    probe = socket.socket()
    try:
        probe.bind((host, settings.https_port))
    except OSError as exc:
        tls.serving["error"] = f"port {settings.https_port} is in use by another program ({exc.strerror or exc})"
        logger.warning("HTTPS not started: %s", tls.serving["error"])
        return None
    finally:
        probe.close()
    names = [n for n in (localname.hostname(settings.site_name) if settings.site_name else "",
                         host_mod.hostname(), "localhost") if n]
    try:
        files = tls.ensure(settings.tls_dir, common_name=names[0], names=names,
                           addresses=[*host_mod.lan_addresses(), "127.0.0.1"])
    except (OSError, ValueError) as exc:
        tls.serving["error"] = f"the certificate could not be made ({exc})"
        logger.warning("HTTPS not started: %s", tls.serving["error"])
        return None
    tls.serving["port"] = settings.https_port
    return files


async def _serve_together(servers) -> None:
    """Runs the HTTP and HTTPS listeners in one loop; when either is asked to stop, both stop."""
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    async def tie():
        while not any(s.should_exit for s in servers) and not any(t.done() for t in tasks):
            await asyncio.sleep(0.2)
        for s in servers:
            s.should_exit = True
    await asyncio.gather(tie(), *tasks)
