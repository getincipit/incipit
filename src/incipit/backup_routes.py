"""HTTP routes for Settings → Backups: where backups go, the last result, the snapshots kept, backing
up now, and restoring a snapshot into a new folder."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from . import accounts, backup, datafolder, relocate, restore


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _paths(settings) -> dict:
    """The places backups actually use: the file's, with any environment override applied, as serve does."""
    folder = Path(settings.data_folder)
    return datafolder.resolved_paths(folder, datafolder.load(folder))


# A place set by an environment variable is the operator's; the page shows it but cannot change it.
FROM_ENVIRONMENT = {"destination": "INCIPIT_BACKUP_DIR", "second_copy": "INCIPIT_BACKUP_MIRROR"}


def _set_by_environment() -> dict:
    return {key: variable for key, variable in FROM_ENVIRONMENT.items() if os.environ.get(variable)}


def _same_drive(a: Path, b: Path) -> bool:
    return a.resolve().anchor.lower() == b.resolve().anchor.lower()


def payload(settings, running: bool) -> dict:
    if not settings.data_folder:
        return {"configured": False}
    paths = _paths(settings)
    destination, second = paths["backups"], paths["backup_mirror"]
    status = backup.read_status(paths["logs"]) or {}
    try:
        kept = restore.snapshots(destination) if destination.is_dir() else []
    except OSError:
        kept = []
    return {
        "configured": True,
        "data_folder": settings.data_folder,
        "destination": str(destination),
        "second_copy": str(second) if second else "",
        "same_drive": _same_drive(destination, Path(settings.data_folder)),
        "available": destination.is_dir(),
        "last": {"at": status.get("at"), "ok": status.get("ok"), "detail": status.get("detail", ""),
                 "last_success_at": status.get("last_success_at")},
        "snapshots": kept,
        "running": running,
        "set_by_environment": _set_by_environment(),
    }


def install(app, settings) -> None:
    state = {"running": False}

    def run_now() -> None:
        paths = _paths(settings)
        state["running"] = True
        try:
            backup.run_backup(paths["database"], paths["backups"], data_dir=settings.data_folder,
                              status_dir=paths["logs"], mirror_dir=paths["backup_mirror"],
                              export_dir=paths["exports"])
        except Exception:  # noqa: BLE001 - the status file records why; the page reads it
            pass
        finally:
            state["running"] = False

    @app.get("/api/backups")
    def get_backups():
        return payload(settings, state["running"])

    @app.put("/api/backups")
    async def put_backups(request: Request):
        """The owner's choice of where backups go; checked, saved, and used from the next backup on."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner chooses where backups go")
        if not settings.data_folder:
            return _error(409, "this site is not run from a data folder")
        body = await request.json()
        fixed = {key: variable for key, variable in _set_by_environment().items() if key in body}
        if fixed:
            return _error(409, "set by the environment variable " + ", ".join(fixed.values())
                          + " where the site is started; change it there")
        folder = Path(settings.data_folder)
        config = datafolder.load(folder, with_env=False)
        for key, field in (("destination", "backups"), ("second_copy", "backup_mirror")):
            if key not in body:
                continue
            raw = str(body[key] or "").strip()
            if not raw:
                config["paths"][field] = "backups" if field == "backups" else ""
                continue
            place = Path(raw).expanduser()
            if not place.is_absolute():
                return _error(400, "give the whole path, such as E:\\Incipit Backups")
            inside = place.resolve() == folder.resolve() or folder.resolve() in place.resolve().parents
            if place.resolve() == folder.resolve() or (field == "backup_mirror" and inside):
                return _error(400, "choose a folder outside the data folder, ideally on another drive")
            try:
                backup.check_destination(place)
            except backup.DestinationUnavailable as exc:
                return _error(400, str(exc))
            config["paths"][field] = str(place)
        try:
            datafolder.save(folder, config)
        except datafolder.ConfigError as exc:
            return _error(400, str(exc))
        restart = getattr(request.app.state, "restart_backups", None)
        if restart is not None:
            restart()
        return payload(settings, state["running"])

    @app.post("/api/backups/run")
    def post_run():
        if not settings.data_folder:
            return _error(409, "this site is not run from a data folder")
        if not state["running"]:
            threading.Thread(target=run_now, name="incipit-backup-now", daemon=True).start()
            state["running"] = True
        return payload(settings, True)

    @app.post("/api/backups/restore")
    async def post_restore(request: Request):
        """Records a restore into a new folder and restarts the site, which carries it out."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner restores a backup")
        if not settings.data_folder:
            return _error(409, "this site is not run from a data folder")
        body = await request.json()
        paths = _paths(settings)
        names = {s["name"] for s in restore.snapshots(paths["backups"])} if paths["backups"].is_dir() else set()
        if body.get("snapshot") and body["snapshot"] not in names:
            return _error(400, "no such snapshot in the backups")
        try:
            target = relocate.request_restore(settings.data_folder, paths["backups"], body.get("snapshot") or "",
                                              body.get("target"))
        except (relocate.MoveError, restore.RestoreError) as exc:
            return _error(400, str(exc))
        restart = getattr(request.app.state, "request_restart", None)
        if restart is not None:
            threading.Timer(0.5, restart).start()
        return {**payload(settings, state["running"]), "restore_to": str(target), "restarting": restart is not None}
