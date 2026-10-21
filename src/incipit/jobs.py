"""Drafting and split proposals as jobs: recorded before they start, so they outlive the request.

A route enqueues a job that says what to do (its kind, folder, batch and documents), a worker in
this process runs it, and the route waits for the answer as it always did. When the browser goes
away the job carries on; when the site stops mid-job, the next start queues it again, and the
handler only touches what is still undone, so nothing is drafted twice. The document in flight at
the stop is recorded in the ledger as an ambiguous call, because it may have been billed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import ledger

logger = logging.getLogger(__name__)

KINDS = ("draft", "split")
STATES = ("queued", "running", "succeeded", "failed", "cancelled")
FINISHED = ("succeeded", "failed", "cancelled")
WORKERS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('draft', 'split')),
    folder_id TEXT,
    batch_id TEXT,
    document_ids_json TEXT NOT NULL DEFAULT '[]',
    label TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    current_document TEXT,
    requested_by INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""


def init_jobs(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _shape(row) -> dict:
    job = dict(row)
    job["document_ids"] = json.loads(job.pop("document_ids_json") or "[]")
    job["result"] = json.loads(job.pop("result_json") or "{}")
    job["cancel_requested"] = bool(job["cancel_requested"])
    return job


def get(db_path: str, job_id: int) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _shape(row) if row else None
    finally:
        conn.close()


def listing(db_path: str, folder_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = _connect(db_path)
    try:
        if folder_id:
            rows = conn.execute("SELECT * FROM jobs WHERE folder_id = ? ORDER BY id DESC LIMIT ?", (folder_id, limit))
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [_shape(r) for r in rows.fetchall()]
    finally:
        conn.close()


def enqueue(db_path: str, kind: str, *, folder_id: str | None = None, batch_id: str | None = None,
            document_ids: list[str] | None = None, label: str = "", requested_by: int | None = None) -> dict:
    """A new queued job, or the one already queued or running for the same folder or batch."""
    if kind not in KINDS:
        raise ValueError(f"no job kind {kind!r}")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        key_column, key = ("folder_id", folder_id) if kind == "draft" and folder_id else ("batch_id", batch_id)
        if key:
            existing = conn.execute(
                f"SELECT * FROM jobs WHERE kind = ? AND {key_column} = ? AND state IN ('queued', 'running') "
                f"ORDER BY id LIMIT 1", (kind, key)).fetchone()
            if existing is not None:
                conn.commit()
                return _shape(existing)
        ids = list(document_ids or [])
        cursor = conn.execute(
            "INSERT INTO jobs (kind, folder_id, batch_id, document_ids_json, label, total, requested_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, folder_id, batch_id, json.dumps(ids), label, len(ids), requested_by, _now()))
        conn.commit()
        return get(db_path, cursor.lastrowid)
    finally:
        conn.close()


def _update(db_path: str, job_id: int, **values) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", [*values.values(), job_id])
        conn.commit()
    finally:
        conn.close()


def cancel(db_path: str, job_id: int) -> dict | None:
    """A queued job ends now; a running one ends after its current document."""
    job = get(db_path, job_id)
    if job is None or job["state"] in FINISHED:
        return job
    if job["state"] == "queued":
        _update(db_path, job_id, state="cancelled", finished_at=_now(), cancel_requested=1)
    else:
        _update(db_path, job_id, cancel_requested=1)
    return get(db_path, job_id)


def retry(db_path: str, job_id: int) -> dict | None:
    """A failed or cancelled job queued again; its handler picks up what is still undone."""
    job = get(db_path, job_id)
    if job is None or job["state"] not in ("failed", "cancelled"):
        return job
    _update(db_path, job_id, state="queued", cancel_requested=0, error_kind="", error_text="", finished_at=None,
            started_at=None)
    return get(db_path, job_id)


def recover(db_path: str) -> int:
    """At start: jobs the last run left running are queued again, and a document that was in flight
    is recorded as an ambiguous call, since it may have been billed."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM jobs WHERE state = 'running'").fetchall()
        for row in rows:
            if row["current_document"]:
                model = conn.execute("SELECT model FROM model_backends WHERE id = 1").fetchone()
                model = model["model"] if model else ""
                cost = ledger.estimate(conn, row["kind"], model)
                now = _now()
                conn.execute(
                    "INSERT INTO model_calls (started_at, finished_at, purpose, job_id, document_id, batch_id, model, "
                    "cost_usd, cost_source, outcome, error_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'estimated', "
                    "'ambiguous', 'the site stopped while this call was in flight')",
                    (row["started_at"] or now, now, row["kind"], row["id"], row["current_document"], row["batch_id"],
                     model, cost))
        conn.execute("UPDATE jobs SET state = 'queued', current_document = NULL, started_at = NULL "
                     "WHERE state = 'running'")
        conn.commit()
        return len(rows)
    finally:
        conn.close()


class Stop(Exception):
    """Raised by a handler's progress callback when the job was cancelled."""


class AccountProblem(Exception):
    """The key or the balance, not the work: the job fails and nothing more is attempted."""


class Worker:
    """Runs queued jobs in this process, a few at a time, never two for the same folder or batch."""

    def __init__(self, db_path: str, handlers: dict, workers: int = WORKERS, poll: float = 0.5):
        self.db_path = db_path
        self.handlers = handlers
        self.workers = workers
        self.poll = poll
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._claim_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def start(self) -> "Worker":
        recovered = recover(self.db_path)
        if recovered:
            logger.info("queued %d job(s) the last run left unfinished", recovered)
        for n in range(self.workers):
            thread = threading.Thread(target=self._loop, name=f"incipit-jobs-{n}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def nudge(self) -> None:
        self._wake.set()

    def _claim(self) -> dict | None:
        with self._claim_lock:
            conn = _connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM jobs q WHERE state = 'queued' AND NOT EXISTS ("
                    " SELECT 1 FROM jobs r WHERE r.state = 'running' AND ("
                    "  (q.folder_id IS NOT NULL AND r.folder_id = q.folder_id) OR"
                    "  (q.batch_id IS NOT NULL AND r.batch_id = q.batch_id))) ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    conn.commit()
                    return None
                conn.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ?", (_now(), row["id"]))
                conn.commit()
                return get(self.db_path, row["id"])
            finally:
                conn.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._claim()
            if job is None:
                self._wake.wait(self.poll)
                self._wake.clear()
                continue
            self.run(job)

    def run(self, job: dict) -> None:
        execute(self.db_path, self.handlers, job)


class Busy(Exception):
    """The same folder is already being worked on outside the job system (activity.exclusive)."""


def execute(db_path: str, handlers: dict, job: dict) -> None:
    """Runs one claimed job to its end and records how it ended."""
    handler = handlers.get(job["kind"])
    job_id = job["id"]

    def progress(done: int | None = None, current: str | None = None, total: int | None = None):
        values = {"current_document": current}
        if done is not None:
            values["done"] = done
        if total is not None:
            values["total"] = total
        _update(db_path, job_id, **values)
        if (get(db_path, job_id) or {}).get("cancel_requested"):
            raise Stop()

    try:
        if handler is None:
            raise RuntimeError(f"no handler for {job['kind']} jobs")
        with ledger.context(purpose=job["kind"], job_id=job_id, batch_id=job["batch_id"]):
            result = handler(job, progress) or {}
        state, kind, text = "succeeded", "", ""
    except Stop:
        result, state, kind, text = {}, "cancelled", "", "cancelled"
    except AccountProblem as exc:
        result, state, kind, text = {}, "failed", "account", str(exc)
    except Busy as exc:
        result, state, kind, text = {}, "failed", "busy", str(exc)
    except Exception as exc:  # noqa: BLE001 - a job's failure is recorded, never lost
        logger.exception("job %s (%s) failed", job_id, job["kind"])
        result, state, kind, text = {}, "failed", "error", str(exc)
    _update(db_path, job_id, state=state, finished_at=_now(), current_document=None,
            error_kind=kind, error_text=text[:2000], result_json=json.dumps(result))


def claim(db_path: str, job_id: int) -> dict | None:
    """Takes this queued job for the caller to run, or None when someone else already has it."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        taken = conn.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ? AND state = 'queued'",
                             (_now(), job_id)).rowcount
        conn.commit()
    finally:
        conn.close()
    return get(db_path, job_id) if taken else None


def run_and_wait(db_path: str, handlers: dict, job: dict, timeout: float | None = None) -> dict:
    """What a route does: runs its job here when nobody else has it (the request's thread carries on
    even if the browser goes away), else waits for whoever does."""
    claimed = claim(db_path, job["id"]) if job["state"] == "queued" else None
    if claimed is not None:
        execute(db_path, handlers, claimed)
    return wait(db_path, job["id"], timeout)


def wait(db_path: str, job_id: int, timeout: float | None = None, poll: float = 0.25) -> dict:
    """Blocks until the job finishes (or the timeout passes), then returns it."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        job = get(db_path, job_id)
        if job is None or job["state"] in FINISHED:
            return job
        if deadline is not None and time.monotonic() > deadline:
            return job
        time.sleep(poll)
