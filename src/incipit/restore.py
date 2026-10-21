"""Rebuilding a data folder from a backup, always into a new folder, never over the running site.

A backup holds verified database snapshots, each with a manifest of the bucket files it refers to,
an additive copy of the bucket and the exports, and the site's incipit.toml. A restore takes one
snapshot, copies everything beside it into a new folder, checks every file the manifest names, and
writes RESTORE-REPORT.txt saying what (if anything) the backup lacked and which documents that
touches. Nothing missing stops the rest from being restored.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path


from . import backup, datafolder, export_archive

REPORT_NAME = "RESTORE-REPORT.txt"


class RestoreError(ValueError):
    """A restore that cannot start; the message says why and what to do."""


def snapshots(backup_dir: str | Path) -> list[dict]:
    """The backup's snapshots, newest first, as Settings → Backups lists them."""
    found = []
    for moment, path in backup.list_snapshots(Path(backup_dir)):
        found.append({"name": path.name, "at": moment.isoformat(), "bytes": path.stat().st_size,
                      "manifest": backup.manifest_path(path).is_file()})
    return found


def _choose(backup_dir: Path, name: str | None) -> Path:
    listed = backup.list_snapshots(backup_dir)
    if not listed:
        raise RestoreError(f"{backup_dir} holds no snapshot; point at the folder that Settings > Backups writes to")
    if name:
        chosen = next((path for _, path in listed if path.name == name), None)
        if chosen is None:
            raise RestoreError(f"{backup_dir} has no snapshot named {name}")
        return chosen
    # The newest that opens; an unreadable one is passed over rather than restored.
    for _, path in listed:
        if backup.verify_snapshot(path)[0]:
            return path
    raise RestoreError(f"none of the snapshots in {backup_dir} passes its check")


def check_target(target: str | Path) -> Path:
    target = Path(target).expanduser()
    if not target.is_absolute():
        raise RestoreError("give the whole path of the new folder, such as D:\\Incipit Restored")
    if target.exists() and any(target.iterdir()):
        raise RestoreError(f"{target} is not empty; restore into a new or empty folder")
    return target


def _config(backup_dir: Path) -> dict:
    """The backed-up incipit.toml with the folder-relative paths kept and the backups pointed back at
    the backup restored from, so the restored site goes on protecting itself in the same place."""
    saved = backup_dir / backup.CONFIG_DIRNAME / datafolder.CONFIG_NAME
    try:
        config = tomllib.loads(saved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    paths = config.setdefault("paths", {})
    for key in ("database", "bucket", "exports", "logs", "tls", "tunnel"):
        # A restored folder holds its own copies, whatever absolute place the old one used.
        if key in paths and Path(str(paths[key])).is_absolute():
            paths[key] = datafolder.DEFAULTS["paths"][key]
    return config


def restore(backup_dir: str | Path, target: str | Path, snapshot: str | None = None) -> dict:
    """Builds `target` from the backup; returns what was restored and what the backup lacked."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        raise RestoreError(f"{backup_dir} does not exist; reconnect the drive it is on")
    chosen = _choose(backup_dir, snapshot)
    ok, detail = backup.verify_snapshot(chosen)
    if not ok:
        raise RestoreError(f"the snapshot {chosen.name} does not pass its check ({detail})")
    target = check_target(target)
    try:
        return _build(backup_dir, chosen, target)
    except BaseException:
        # The target was new or empty, so all of it is the half-built restore; a retry needs it gone.
        shutil.rmtree(target, ignore_errors=True)
        raise


def _build(backup_dir: Path, chosen: Path, target: Path) -> dict:
    config = _config(backup_dir)
    target.mkdir(parents=True, exist_ok=True)
    datafolder.initialise(target)
    written = datafolder.load(target, with_env=False)
    for section, values in config.items():
        if section in written and isinstance(values, dict):
            written[section].update({k: v for k, v in values.items() if k in written[section]})
    datafolder.save(target, written)
    paths = datafolder.resolved_paths(target, written)

    database = paths["database"]
    shutil.copy2(chosen, database)
    for sidecar in ("-wal", "-shm"):
        Path(str(database) + sidecar).unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(database)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RestoreError("the restored database did not pass its check; try an older snapshot")

    copied = backup.copy_tree_additive(backup_dir / backup.BUCKET_DIRNAME, paths["bucket"])
    exports_copied = 0
    if (backup_dir / backup.EXPORTS_DIRNAME).is_dir():
        exports_copied = backup.copy_tree_additive(backup_dir / backup.EXPORTS_DIRNAME,
                                                   export_archive.source_root(target, paths["exports"]))

    missing = _missing(backup.manifest_path(chosen), paths["bucket"])
    report = {"snapshot": chosen.name, "target": str(target), "files_copied": copied,
              "exports_copied": exports_copied, "missing": missing,
              "manifest": backup.manifest_path(chosen).is_file()}
    (target / REPORT_NAME).write_text(describe(report, backup_dir), encoding="utf-8")
    return report


def _missing(manifest: Path, bucket: Path) -> list[dict]:
    """Each manifest entry the restored bucket lacks, or holds at another size."""
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        return []
    lacking = []
    for entry in entries:
        path = bucket / entry["key"]
        if not path.is_file() or (entry.get("size") is not None and path.stat().st_size != entry["size"]):
            lacking.append(entry)
    return lacking


def describe(report: dict, backup_dir: Path) -> str:
    """The report a person reads: what came back, and what to look for if something did not."""
    lines = [f"Restored {report['target']}",
             f"from the snapshot {report['snapshot']} in {backup_dir}",
             f"on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.", ""]
    if not report["manifest"]:
        lines.append("This snapshot has no manifest (it was made before manifests existed), so no file was checked.")
    elif not report["missing"]:
        lines.append("Every file the snapshot refers to was in the backup.")
    else:
        lines.append(f"{len(report['missing'])} file(s) the snapshot refers to were not in the backup:")
        for entry in report["missing"]:
            owner = (f"document {entry.get('display_id') or entry.get('document_id')}" if entry.get("document_id")
                     else f"upload batch {entry['batch_id']}" if entry.get("batch_id") else entry.get("kind", "file"))
            lines.append(f"  - {entry['key']}  ({entry.get('kind', 'file')} of {owner})")
        lines += ["", "Everything else was restored. Look for these files in the second backup copy, or in the old",
                  "data folder, and copy them into the bucket folder under the same path."]
    return "\n".join(lines) + "\n"
