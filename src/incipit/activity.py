"""What the pipeline is doing right now, shared across every reviewer on the site."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

_lock = threading.Lock()
_active: dict[str, dict] = {}
_resources: set[str] = set()
_shared_resources: dict[str, int] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def track(kind: str, label: str, detail: str = "", owner_id: int | None = None,
          folder: str = "", remaining: int | None = None, key: str | None = None):
    """Registers one long-running operation for as long as the block runs."""
    job_id = uuid.uuid4().hex[:8]
    # owner_id is who started this.
    entry = {"id": job_id, "kind": kind, "label": label, "detail": detail,
             "started_at": _now(), "owner_id": owner_id, "folder": folder,
             "remaining": remaining, "key": key}
    with _lock:
        _active[job_id] = entry
    try:
        yield entry
    finally:
        with _lock:
            _active.pop(job_id, None)


def set_detail(entry: dict, detail: str) -> None:
    """Updates an in-flight job's commentary (e.g. "12 of 40 pages examined")."""
    with _lock:
        entry["detail"] = detail


def set_remaining(entry: dict, remaining: int | None) -> None:
    """How many documents this job still has to do."""
    with _lock:
        entry["remaining"] = remaining


def snapshot() -> list[dict]:
    """Every running operation, oldest first."""
    now = _now()
    with _lock:
        entries = list(_active.values())
    entries.sort(key=lambda e: e["started_at"])
    return [
        {
            "id": e["id"],
            "kind": e["kind"],
            "label": e["label"],
            "detail": e["detail"],
            "folder": e.get("folder", ""),
            "remaining": e.get("remaining"),
            # Carried through because scoping.visible_activity filters on it.
            "owner_id": e["owner_id"],
            "started_at": e["started_at"].isoformat(),
            "elapsed_seconds": round((now - e["started_at"]).total_seconds(), 1),
        }
        for e in entries
    ]


# Failures of work nobody is waiting on, such as the draft an accept starts after its response.
_problems: list[dict] = []
_PROBLEMS_KEPT = 10


def report_problem(kind: str, message: str) -> None:
    """Keep a background failure where the next activity poll can show it."""
    with _lock:
        _problems.append({"id": uuid.uuid4().hex[:8], "kind": kind, "message": message,
                          "at": _now().isoformat()})
        del _problems[:-_PROBLEMS_KEPT]


def problems() -> list[dict]:
    """The recent background failures, oldest first."""
    with _lock:
        return [dict(problem) for problem in _problems]


class AlreadyRunning(RuntimeError):
    """Raised by `exclusive` when an operation of the same kind is already in flight."""

    def __init__(self, kind: str, running: dict):
        self.kind = kind
        self.running = running
        super().__init__(f"{kind} is already running: {running.get('label', '')}")


class ResourceBusy(RuntimeError):
    """Raised when two requests try to mutate the same durable resource at once."""


@contextmanager
def resource_lock(key: str, *, shared: bool = False):
    """Non-blocking shared/exclusive lock for writes and long-running replacements.

    Ordinary saves use the shared side: SQLite serializes their transactions and role-specific
    verdicts can both succeed. A redraft uses the exclusive side, so it cannot overlap any save and
    no save can put a stale form over the replacement while the model is running.
    """
    with _lock:
        if key in _resources or (not shared and _shared_resources.get(key, 0)):
            raise ResourceBusy(key)
        if shared:
            _shared_resources[key] = _shared_resources.get(key, 0) + 1
        else:
            _resources.add(key)
    try:
        yield
    finally:
        with _lock:
            if shared:
                remaining = _shared_resources.get(key, 1) - 1
                if remaining:
                    _shared_resources[key] = remaining
                else:
                    _shared_resources.pop(key, None)
            else:
                _resources.discard(key)


@contextmanager
def exclusive(kind: str, label: str, detail: str = "", owner_id: int | None = None,
              folder: str = "", remaining: int | None = None, key: str | None = None,
              blocks: tuple[str, ...] = ()):
    """`track`, but refuses to start when a job it would collide with is already running.

    Two jobs collide when they share a `kind` and a `key`. A `key` of None means "everything of
    this kind" -- it collides with any of them, and any of them collides with it -- which is what a
    run that draws from the whole pending queue needs. Drafting one folder names that folder, so
    another folder may be drafted at the same time; drafting the same one twice may not.
    `blocks` names other kinds this job also collides with on the same key, in both directions.
    """
    job_id = uuid.uuid4().hex[:8]
    # owner_id is who started this.
    entry = {"id": job_id, "kind": kind, "label": label, "detail": detail,
             "started_at": _now(), "owner_id": owner_id, "folder": folder,
             "remaining": remaining, "key": key, "blocks": tuple(blocks)}
    with _lock:
        for other in _active.values():
            if (other["kind"] != kind and other["kind"] not in blocks
                    and kind not in other.get("blocks", ())):
                continue
            if key is None or other.get("key") is None or other["key"] == key:
                raise AlreadyRunning(kind, dict(other))
        _active[job_id] = entry
    try:
        yield entry
    finally:
        with _lock:
            _active.pop(job_id, None)


def clear() -> None:
    """Test helper -- drops every tracked job and reported problem."""
    with _lock:
        _active.clear()
        _problems.clear()
        _resources.clear()
        _shared_resources.clear()
