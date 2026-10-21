"""Operational API routes: health, live activity, and queue counts."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import activity, backup, db, scoping
from .config import Settings

STALE_BACKUP_SECONDS = 20 * 60


def _database_reachable(db_path: str) -> bool:
    try:
        with db.connect(db_path) as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception:  # noqa: BLE001 - any failure here is the thing being reported
        return False


def _commit() -> str:
    """The version shipped with the build (VERSION), else the checkout's commit, else unknown."""
    from .assets import asset_directory

    version = asset_directory("VERSION")
    if version.is_file():
        return version.read_text(encoding="utf-8").strip()
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=2).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _named(settings: Settings, entries: list[dict]) -> list[dict]:
    ids = {entry.get("owner_id") for entry in entries if entry.get("owner_id")}
    names = {}
    if ids:
        try:
            with db.connect(settings.db_path) as conn:
                placeholders = ",".join("?" * len(ids))
                for row in conn.execute(
                    f"SELECT id, display_name FROM users WHERE id IN ({placeholders})", tuple(ids)
                ):
                    names[row["id"]] = row["display_name"]
        except Exception:  # noqa: BLE001 - a missing name never hides a running job
            pass
    return [dict(entry, owner_name=names.get(entry.get("owner_id"), "")) for entry in entries]


def install(
    app: FastAPI,
    settings: Settings,
    backup_status_dir: str,
    viewer: Callable[[Request], object | None],
) -> None:
    """Install routes owned by deployment/operations rather than the review workflow."""

    def pending_for(request: Request) -> dict[str, int]:
        """Pending counts within the same folder boundary as the review queues."""
        with db.connect(settings.db_path) as conn:
            new_ids = db.get_new_pending_document_ids(conn)
            new_visible = scoping.visible_folder_ids(conn, viewer(request), "new")
            if new_visible is not None:
                new_ids = [item for item in new_ids
                           if db.folder_of_document(conn, item) in new_visible]
        return {"new": len(new_ids)}

    @app.get("/health")
    def health():
        checks = {"database": _database_reachable(settings.db_path), "backup": None}
        status = backup.read_status(backup_status_dir)
        if status and status.get("last_success_at"):
            try:
                last = datetime.fromisoformat(status["last_success_at"])
                checks["backup"] = int((datetime.now(timezone.utc) - last).total_seconds())
            except (TypeError, ValueError):
                pass
        healthy = checks["database"] and (
            checks["backup"] is None or checks["backup"] < STALE_BACKUP_SECONDS
        )
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "commit": _commit(), **checks},
            status_code=200 if healthy else 503,
        )

    @app.get("/api/activity")
    def get_activity(request: Request):
        pending = pending_for(request)
        user = viewer(request)
        visible = scoping.visible_activity(user, activity.snapshot())
        return {
            "active": _named(settings, visible),
            "pending": pending,
            # Admin-only: a drafting failure can name the account's balance or spend.
            "problems": activity.problems() if user is None or scoping.is_admin(user) else [],
        }

    @app.get("/api/pending-counts")
    def get_pending_counts(request: Request):
        pending = pending_for(request)
        return {"new_awaiting_drafting": pending["new"]}
