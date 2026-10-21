"""Where this computer's site keeps its data, and moving it somewhere else.

The installer records the chosen data folder in a small location file in the user's own settings
folder; the shortcuts and the start-at-login entry run the program without a path and it reads the
location from there, so moving the data never breaks them. A move is asked for while the site
runs and carried out the next time it starts, before the database is opened: the folder is copied,
the copy is checked, and only then does the location file point at it. The old folder is left
exactly as it was, with a note saying where its data went.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import sqlite3
import tomllib
from pathlib import Path

import tomli_w

from . import datafolder

LOCATION_NAME = "location.toml"
MOVED_NOTE = "MOVED-TO.txt"
FAILED_NOTE = "MOVE-FAILED.txt"
# Room the move must leave free beyond the copy itself, so the disk is not filled to the brim.
SPARE_BYTES = 200 * 1024 * 1024


class MoveError(ValueError):
    """A move that cannot be made; the message says why and what to do instead."""


def settings_dir() -> Path:
    """The user's own settings folder for Incipit, which outlives a reinstall."""
    override = os.environ.get("INCIPIT_SETTINGS_DIR")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Incipit"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Incipit"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "incipit"


def location_path() -> Path:
    return settings_dir() / LOCATION_NAME


def read_location() -> dict:
    try:
        return tomllib.loads(location_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return {}


def _write_location(values: dict) -> None:
    path = location_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({k: v for k, v in values.items() if v not in (None, "")}), encoding="utf-8")


def remember(folder: str | Path) -> None:
    """Records `folder` as this computer's data folder, keeping any move already asked for."""
    location = read_location()
    location["data_folder"] = str(Path(folder).resolve())
    _write_location(location)


def remembered() -> Path | None:
    value = read_location().get("data_folder")
    return Path(value) if value else None


def forwarded(folder: Path) -> Path:
    """The folder a moved-away folder's data went to, so the old copy is never served by mistake."""
    if not (folder / MOVED_NOTE).is_file():
        return folder
    now = remembered()
    if now is not None and now.resolve() != folder.resolve() and (now / datafolder.CONFIG_NAME).is_file():
        return now.resolve()
    raise datafolder.ConfigError(f"{folder} was moved (see {MOVED_NOTE} in it); serve the folder it names")


def failure_note(folder: str | Path) -> str:
    """What went wrong with the last move, until the owner dismisses it."""
    try:
        return (Path(folder) / FAILED_NOTE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def dismiss_failure(folder: str | Path) -> None:
    (Path(folder) / FAILED_NOTE).unlink(missing_ok=True)


# ---------- asking for a move ----------


def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def check_target(current: Path, target: str | Path) -> Path:
    """The folder the data would move to, or MoveError saying why it cannot."""
    raw = str(target or "").strip()
    if not raw:
        raise MoveError("type the folder the data should move to")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise MoveError("give the whole path, such as D:\\Incipit Data")
    target = target.resolve()
    current = current.resolve()
    if target == current:
        raise MoveError("the data is already there")
    if current in target.parents:
        raise MoveError("the new folder cannot be inside the current one")
    if target in current.parents:
        raise MoveError("the new folder cannot hold the current one")
    if target.exists() and any(target.iterdir()):
        raise MoveError(f"{target} is not empty; choose a new or empty folder")
    probe_parent = target if target.exists() else next((p for p in target.parents if p.exists()), None)
    if probe_parent is None:
        raise MoveError(f"{target.anchor or target} does not exist on this computer")
    try:
        free = shutil.disk_usage(probe_parent).free
    except OSError as exc:
        raise MoveError(f"{probe_parent} cannot be read ({exc.strerror or exc})") from exc
    needed = folder_size(current) + SPARE_BYTES
    if free < needed:
        raise MoveError(f"{probe_parent} has {free // 2**20} MB free; the data needs about {needed // 2**20} MB")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise MoveError(f"{target} cannot be written to ({exc.strerror or exc})") from exc
    return target


def request(current: str | Path, target: str | Path) -> Path:
    """Checks the target and records the move, which the next start carries out."""
    current = Path(current)
    checked = check_target(current, target)
    location = read_location()
    location["data_folder"] = str(current.resolve())
    location["move_to"] = str(checked)
    _write_location(location)
    return checked


def pending() -> Path | None:
    value = read_location().get("move_to")
    return Path(value) if value else None


def request_restore(current: str | Path, backup_dir: str | Path, snapshot: str, target: str | Path) -> Path:
    """Records a restore from Settings into a new folder, which the next start carries out."""
    from . import restore
    checked = restore.check_target(str(target or "").strip() or ".")
    if not Path(backup_dir).is_dir():
        raise MoveError(f"{backup_dir} is not available; reconnect the drive the backups are on")
    location = read_location()
    location.update(data_folder=str(Path(current).resolve()), move_to=str(checked),
                    restore_from=str(Path(backup_dir).resolve()), restore_snapshot=snapshot or "")
    _write_location(location)
    return checked


def restore_pending() -> dict | None:
    location = read_location()
    if not location.get("restore_from") or not location.get("move_to"):
        return None
    return {"backup": location["restore_from"], "snapshot": location.get("restore_snapshot") or None,
            "target": location["move_to"]}


def cancel() -> None:
    location = read_location()
    for key in ("move_to", "restore_from", "restore_snapshot"):
        location.pop(key, None)
    _write_location(location)


# ---------- carrying it out ----------


def _apply_restore(current: Path, asked: dict) -> Path:
    """Builds the new folder from the backup and runs from it; on failure, stays and says why."""
    from . import restore
    try:
        report = restore.restore(asked["backup"], asked["target"], asked["snapshot"])
    except (restore.RestoreError, OSError, sqlite3.Error) as exc:
        cancel()
        (current / FAILED_NOTE).write_text(f"Restoring {asked['snapshot'] or 'the newest snapshot'} from "
                                           f"{asked['backup']} into {asked['target']} failed: {exc}\n"
                                           f"The site kept running from this folder.\n", encoding="utf-8")
        return current
    _write_location({"data_folder": report["target"]})
    return Path(report["target"]).resolve()


def _same_files(source: Path, copy: Path) -> list[str]:
    """Every file of `source` present in `copy` with the same size; returns what differs."""
    problems = []
    for path in source.rglob("*"):
        if path.is_file():
            twin = copy / path.relative_to(source)
            if not twin.is_file() or twin.stat().st_size != path.stat().st_size:
                problems.append(str(path.relative_to(source)))
    return problems


def _database_ok(path: Path) -> bool:
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def apply_pending(current: Path) -> Path:
    """Moves the data if a move was asked for, returning the folder to serve; on any failure the old one."""
    target = pending()
    if target is None:
        return current
    current = current.resolve()
    asked = restore_pending()
    if asked is not None:
        return _apply_restore(current, asked)
    try:
        # A database left mid-write would not copy faithfully; the site is stopped, so folding the
        # write-ahead log back now leaves one file to copy.
        database = current / datafolder.load(current, with_env=False)["paths"]["database"]
        if database.is_file():
            with contextlib.closing(sqlite3.connect(database)) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copytree(current, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(MOVED_NOTE, FAILED_NOTE))
        problems = [p for p in _same_files(current, target) if Path(p).name not in (MOVED_NOTE, FAILED_NOTE)]
        copied_db = target / database.relative_to(current)
        if problems or (database.is_file() and not _database_ok(copied_db)):
            raise MoveError("the copy did not match: " + ", ".join(problems[:5] or ["the database"]))
    except (OSError, MoveError, sqlite3.Error) as exc:
        cancel()
        # The target was new or empty when the move was asked for, so all of it is the partial copy.
        shutil.rmtree(target, ignore_errors=True)
        note = current / FAILED_NOTE
        note.write_text(f"Moving the data to {target} failed and was undone: {exc}\n"
                        f"The site kept running from this folder.\n", encoding="utf-8")
        return current
    (current / MOVED_NOTE).write_text(
        f"This folder's data was moved to {target}.\nThe site now runs from there. Once you have checked it, "
        f"this folder can be deleted.\n", encoding="utf-8")
    _write_location({"data_folder": str(target)})
    return target
