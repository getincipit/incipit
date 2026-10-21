"""Completed-folder exports: consistent snapshots, host deliverables and bounded jobs.

Review routes own verdicts; this module owns eligibility and generation. export_archive owns
publication/replication. No HTTP response from this workflow contains a downloadable file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from . import accounts, collab, db, docid, export_archive, imaging, scoping, tabs
from .export import effective_rows, export_new_csv
from .storage import BucketStorage

logger = logging.getLogger(__name__)


def records(conn, workflow, folder_id):
    """Folder-local source records, including review state and image provenance."""
    if workflow not in accounts.WORKFLOWS:
        raise ValueError("unknown export workflow")
    rows = conn.execute("SELECT * FROM new_documents WHERE folder_id = ? ORDER BY display_id",
                        (folder_id,)).fetchall()
    result = []
    for row in rows:
        item = {"document": dict(row),
                "pages": [dict(p) for p in db.get_new_document_pages(conn, row["document_id"])]}
        # The reviewer's orientation travels with each page, so a turned page changes the export's version.
        turned = db.page_orientations(conn, workflow, row["document_id"])
        for page in item["pages"]:
            page.update(turned.get(page["sequence_index"], {"rotation": 0, "mirrored": False}))
        result.append(item)
    return result


def eligibility(conn, workflow, folder_id):
    """A folder exports once an admin has reviewed every document; a reviewer's accepts do not gate it."""
    if workflow not in accounts.WORKFLOWS:
        raise ValueError("unknown export workflow")
    docid.require_safe_id(folder_id, "folder id")
    return collab.check_can_approve(conn, folder_id, workflow)["total"]


def ready_for(conn, workflow, document_id) -> bool:
    """Whether the document's folder could be exported right now."""
    folder_id = scoping.document_folder_id(conn, document_id, workflow)
    try:
        return folder_id is not None and eligibility(conn, workflow, folder_id) > 0
    except (ValueError, collab.NotReady):
        return False


def completion(conn, workflow, document_id, was_ready):
    """The prompt follows only the review that completed the folder, never an autosave or a re-review."""
    if was_ready or not ready_for(conn, workflow, document_id):
        return None
    return {"workflow": workflow, "folder_id": scoping.document_folder_id(conn, document_id, workflow)}


def fingerprint(conn, workflow, folder_id, items=None):
    # Copy the container: fingerprint-only metadata must never enter the deliverable records.
    source = list(items) if items is not None else records(conn, workflow, folder_id)
    source.append({"batches": [dict(r) for r in conn.execute(
        "SELECT * FROM new_batches WHERE folder_id = ? ORDER BY batch_id", (folder_id,))]})
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def folder_name(conn, workflow, folder_id):
    """The folder's display title, which names its export files."""
    return db.folder_title(conn, folder_id) or folder_id


def _json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def build(settings, workflow, folder_id, stage):
    """Build from a private SQLite snapshot; images are streamed, never held in memory."""
    snapshot = stage.parent / "snapshot.db"
    with db.connect(settings.db_path) as source, closing(sqlite3.connect(snapshot)) as target:
        source.backup(target)
    frozen = replace(settings, db_path=str(snapshot))
    with db.connect(str(snapshot)) as conn:
        count = eligibility(conn, workflow, folder_id)
        items = records(conn, workflow, folder_id)
        version = fingerprint(conn, workflow, folder_id, items)
        name = folder_name(conn, workflow, folder_id)
        stem = export_archive.basename(name)
        _, csv_body, _ = export_new_csv(frozen, folder_id)
        (stage / f"{stem}.csv").write_bytes(csv_body)
        effective = {r["document_id"]: {k: v for k, v in r.items() if k != "_registry"}
                     for r in effective_rows(conn, db.get_new_documents_for_export(conn, folder_id))}
        bucket = BucketStorage(settings.bucket_uri)
        # Directories are named by display id; two documents whose display ids differ only in a
        # character the filesystem folds would otherwise share one, so a repeat gets a suffix.
        used: set[str] = set()
        for item in items:
            document_id = item["document"]["document_id"]
            display_id = item["document"]["display_id"] or document_id
            docid.require_safe_id(display_id, "document id")
            directory_name, attempt = display_id, 2
            while directory_name.lower() in used:
                directory_name, attempt = f"{display_id}_{attempt}", attempt + 1
            used.add(directory_name.lower())
            directory = stage / "documents" / directory_name
            if not item["pages"]:
                raise ValueError(f"missing page images for {document_id}")
            item["effective"] = effective[document_id]
            _json(directory / "document.json", item)
            for index, page in enumerate(item["pages"]):
                key = page["bucket_key"]
                if not key:
                    raise ValueError(f"missing image for {document_id}")
                filename = f"{index + 1:04d}_{Path(key).name}"
                target = directory / "images" / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                if page["rotation"] or page["mirrored"]:
                    # Exported as it is read on the site; the bucket's scan stays as it was digitized.
                    target.write_bytes(imaging.orient(bucket.read_bytes(key), page["rotation"], page["mirrored"]))
                else:
                    with bucket.open_stream(key) as reader, target.open("wb") as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
        _json(stage / "export.json", {"workflow": workflow, "folder_id": folder_id,
              "source_version": version, "document_count": count})
        archive = stage / f"{stem}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(stage.rglob("*")):
                if path.is_file() and path != archive:
                    bundle.write(path, path.relative_to(stage).as_posix())
    return name, count, version


class Jobs:
    """Small single-process worker pool; durable status and no model work inside DB write locks."""
    def __init__(self, settings, root, backup_dir=None, mirror_dir=None):
        self.settings, self.root = settings, Path(root)
        self.backup_dir, self.mirror_dir = backup_dir, mirror_dir
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)
        with db.connect(settings.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS folder_export_jobs (job_id TEXT PRIMARY KEY, workflow TEXT, "
                         "folder_id TEXT, state TEXT, detail TEXT NOT NULL DEFAULT '', exported_by TEXT)")
            conn.execute("UPDATE folder_export_jobs SET state = 'failed', detail = 'Server restarted; retry export' "
                         "WHERE state IN ('queued', 'running')")

    def status(self, job_id):
        with db.connect(self.settings.db_path) as conn:
            row = conn.execute("SELECT * FROM folder_export_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def start(self, workflow, folder_id, exported_by):
        with self.lock, db.connect(self.settings.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            eligibility(conn, workflow, folder_id)
            existing = conn.execute("SELECT job_id FROM folder_export_jobs WHERE workflow = ? AND folder_id = ? "
                                    "AND state IN ('queued','running')", (workflow, folder_id)).fetchone()
            if existing:
                return existing["job_id"]
            if not self.slots.acquire(blocking=False):
                raise ValueError("two exports are already running; retry shortly")
            job_id = uuid.uuid4().hex
            try:
                conn.execute("INSERT INTO folder_export_jobs VALUES (?, ?, ?, 'queued', '', ?)",
                             (job_id, workflow, folder_id, exported_by))
                conn.commit()
                threading.Thread(target=self._run, args=(job_id, workflow, folder_id, exported_by), daemon=True).start()
            except BaseException:
                self.slots.release()
                raise
            return job_id

    def _set(self, job_id, state, detail=""):
        with db.connect(self.settings.db_path) as conn:
            conn.execute("UPDATE folder_export_jobs SET state = ?, detail = ? WHERE job_id = ?", (state, detail, job_id))

    def _run(self, job_id, workflow, folder_id, exported_by):
        try:
            self._set(job_id, "running")
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".export-", dir=self.root) as temporary:
                stage = Path(temporary) / "deliverable"
                stage.mkdir()
                name, count, version = build(self.settings, workflow, folder_id, stage)
                with export_archive.TREE_LOCK, db.connect(self.settings.db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    eligibility(conn, workflow, folder_id)
                    if fingerprint(conn, workflow, folder_id) != version:
                        raise ValueError("folder changed during export; review the latest version and retry")
                    # Published under the folder's tab, so two tabs' same-named folders never share a directory.
                    group = tabs.of_folder(conn, folder_id)["slug"]
                    export_archive.publish_directory(self.root, group, folder_id, name, stage)
                    db.record_folder_export(conn, folder_id, workflow, count, exported_by)
            detail = f"Saved to exports/{group}/{export_archive.folder_directory(folder_id)}"
            # Backup failures do not roll back a valid host export; scheduled reconciliation retries.
            for destination in (self.backup_dir, self.mirror_dir):
                if destination:
                    try:
                        export_archive.sync_tree(self.root, Path(destination) / "exports")
                    except (OSError, ValueError):
                        logger.exception("export replication will be retried by scheduled backup")
                        detail = "Saved on host; a backup destination is unavailable. Replication will retry."
            self._set(job_id, "completed", detail)
        except Exception as exc:
            logger.exception("folder export failed: %s/%s", workflow, folder_id)
            self._set(job_id, "failed", str(exc))
        finally:
            self.slots.release()
