"""The call ledger: one row for every response from the model service, never changed, never deleted.

Spend, the caps and the Usage page all read it, so a malformed attempt that was billed counts, and
deleting or redrafting a document never gives budget back. Rows are written by a background writer,
because a call is made while the drafting run may hold the database's write lock; the run's own
context (purpose, job, document) travels with the call through `context`.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PURPOSES = ("draft", "split", "merge", "prompt_test", "carried_over", "other")
OUTCOMES = ("ok", "malformed", "refused", "error", "timeout", "ambiguous")
# What an ambiguous call is assumed to have cost when nothing like it has been recorded yet.
DEFAULT_ESTIMATE = 0.005

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ({", ".join(repr(p) for p in PURPOSES)})),
    job_id INTEGER,
    document_id TEXT,
    batch_id TEXT,
    prompt_version_id INTEGER,
    backend_kind TEXT NOT NULL DEFAULT 'openrouter',
    model TEXT NOT NULL DEFAULT '',
    is_fallback INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0,
    cost_source TEXT NOT NULL CHECK (cost_source IN ('provider', 'estimated', 'none')),
    outcome TEXT NOT NULL CHECK (outcome IN ({", ".join(repr(o) for o in OUTCOMES)})),
    http_status INTEGER,
    error_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_model_calls_started ON model_calls(started_at);
CREATE TRIGGER IF NOT EXISTS model_calls_never_change BEFORE UPDATE ON model_calls
BEGIN SELECT RAISE(ABORT, 'model_calls is insert-only'); END;
CREATE TRIGGER IF NOT EXISTS model_calls_never_go BEFORE DELETE ON model_calls
BEGIN SELECT RAISE(ABORT, 'model_calls is insert-only'); END;
"""


def init_ledger(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _carry_over(conn)


def _carry_over(conn: sqlite3.Connection) -> None:
    """A database from before the ledger keeps the spend its cost columns recorded, as one row."""
    if conn.execute("SELECT 1 FROM model_calls LIMIT 1").fetchone():
        return
    total = 0.0
    for sql in ("SELECT COALESCE(SUM(cost_estimate), 0) FROM new_documents",
                "SELECT COALESCE(SUM(split_cost_estimate), 0) FROM new_batches",
                "SELECT COALESCE(SUM(cost_estimate), 0) FROM deletion_log",
                "SELECT COALESCE(SUM(cost_estimate), 0) FROM prompt_tests"):
        try:
            total += conn.execute(sql).fetchone()[0] or 0.0
        except sqlite3.OperationalError:
            continue
    if total > 0:
        now = _now()
        conn.execute("INSERT INTO model_calls (started_at, finished_at, purpose, cost_usd, cost_source, outcome) "
                     "VALUES (?, ?, 'carried_over', ?, 'provider', 'ok')", (now, now, total))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- the calling context ----------

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("incipit_ledger_context", default={})


@contextlib.contextmanager
def context(**values):
    """Labels every call made inside the block (purpose, job_id, document_id, batch_id, prompt_version_id)."""
    token = _context.set({**_context.get(), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict:
    return dict(_context.get())


# ---------- writing ----------

_queue: queue.Queue = queue.Queue()
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()
_exit_hook = {"registered": False}


def record(db_path: str, **row) -> None:
    """Queues one call's row; the writer inserts it as soon as the database lets it."""
    labels = current_context()
    row = {"purpose": labels.get("purpose", "other"), "job_id": labels.get("job_id"),
           "document_id": labels.get("document_id"), "batch_id": labels.get("batch_id"),
           "prompt_version_id": labels.get("prompt_version_id"), **row}
    if row["purpose"] not in PURPOSES:
        row["purpose"] = "other"
    _queue.put((db_path, row))
    _ensure_writer()


def _ensure_writer() -> None:
    global _writer
    with _writer_lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_write_forever, name="incipit-ledger", daemon=True)
            _writer.start()
            # A daemon thread dies with the process; every billed call queued by then is written first.
            if not _exit_hook["registered"]:
                atexit.register(flush)
                _exit_hook["registered"] = True


COLUMNS = ("started_at", "finished_at", "purpose", "job_id", "document_id", "batch_id", "prompt_version_id",
           "backend_kind", "model", "is_fallback", "input_tokens", "output_tokens", "cost_usd", "cost_source",
           "outcome", "http_status", "error_text")


def _insert(db_path: str, row: dict) -> None:
    values = {"backend_kind": "openrouter", "model": "", "is_fallback": 0, "cost_usd": 0.0, "cost_source": "none",
              "outcome": "ok", "error_text": "", "started_at": _now(), "finished_at": _now(), **row}
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute(f"INSERT INTO model_calls ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                     [values.get(c) for c in COLUMNS])
        conn.commit()
    finally:
        conn.close()


def _write_forever() -> None:
    while True:
        db_path, row = _queue.get()
        for attempt in range(10):
            try:
                _insert(db_path, row)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    logger.error("the ledger could not record a call: %s (%r)", exc, row)
                    break
                # Locked for longer than the connection's own wait: try again rather than lose a billed call.
                logger.warning("the ledger could not record a call yet (%s); retrying", exc)
                threading.Event().wait(min(2 ** attempt, 30))
            except sqlite3.Error as exc:
                logger.error("the ledger could not record a call: %s (%r)", exc, row)
                break
        _queue.task_done()


def flush(timeout: float = 30.0) -> None:
    """Waits until every queued row is written, for a caller about to read spend."""
    if not _queue.unfinished_tasks:
        return
    try:
        _ensure_writer()
        done = threading.Event()
        threading.Thread(target=lambda: (_queue.join(), done.set()), daemon=True).start()
        done.wait(timeout)
    except RuntimeError:
        # At interpreter shutdown no new thread may start; the writer itself is still running.
        deadline = time.monotonic() + timeout
        while _queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)


# ---------- reading ----------

def spent(conn: sqlite3.Connection, since: str | None = None) -> float:
    """Everything recorded, or since an ISO time; queued rows are flushed first unless this connection
    holds a write, which the writer would have to wait behind."""
    if not conn.in_transaction:
        flush()
    if since:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM model_calls WHERE started_at >= ?", (since,))
    else:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM model_calls")
    return float(row.fetchone()[0])


def month_start(now: datetime | None = None, months_back: int = 0) -> str:
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month - months_back
    while month < 1:
        month += 12
        year -= 1
    return datetime(year, month, 1, tzinfo=timezone.utc).isoformat()


def estimate(conn: sqlite3.Connection, purpose: str, model: str) -> float:
    """What a call like this usually costs, for one whose answer never came back."""
    row = conn.execute(
        "SELECT AVG(cost_usd) FROM (SELECT cost_usd FROM model_calls WHERE purpose = ? AND model = ? "
        "AND outcome = 'ok' AND cost_source = 'provider' ORDER BY id DESC LIMIT 50)", (purpose, model)).fetchone()
    return float(row[0]) if row and row[0] is not None else DEFAULT_ESTIMATE


def usage(conn: sqlite3.Connection, recent: int = 50) -> dict:
    """What the Usage page shows."""
    if not conn.in_transaction:
        flush()
    this_month, last_month = month_start(), month_start(months_back=1)

    def grouped(column: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT {column} AS name, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
            f"COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens "
            f"FROM model_calls GROUP BY {column} ORDER BY cost DESC").fetchall()
        return [dict(r) if isinstance(r, sqlite3.Row) else dict(zip(("name", "calls", "cost", "input_tokens",
                                                                       "output_tokens"), r)) for r in rows]
    latest = conn.execute(
        f"SELECT id, {', '.join(COLUMNS)} FROM model_calls WHERE purpose != 'carried_over' ORDER BY id DESC LIMIT ?",
        (recent,)).fetchall()
    keys = ("id", *COLUMNS)
    return {
        "this_month": spent(conn, this_month),
        "last_month": spent(conn, last_month) - spent(conn, this_month),
        "all_time": spent(conn),
        "by_purpose": grouped("purpose"),
        "by_model": grouped("model"),
        "latest": [dict(zip(keys, tuple(r))) for r in latest],
    }
