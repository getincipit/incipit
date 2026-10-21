"""Verified snapshots of the SQLite database, plus an additive copy of the bucket.

The bucket's keys are built from internal ids that never change (see storage.py), so the backup
copies them as they are -- `bucket/records/...`, `bucket/documents/...`, `bucket/uploads/...` --
and restoring is copying them back. Nothing is removed from a backup except what the deletion log
says the site deleted.
"""

from __future__ import annotations

import sqlite3
import hashlib
import json
import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, export_archive, file_locks, site_assets, storage

logger = logging.getLogger(__name__)

# Snapshots are named so they sort chronologically as plain strings and carry their own timestamp
# -- no sidecar metadata to lose, and `ls` is a usable interface during an incident.
SNAPSHOT_PREFIX = "extraction-"
SNAPSHOT_SUFFIX = ".db"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

STATUS_FILENAME = "last-status.json"
MANIFEST_SUFFIX = ".manifest.json"
# The site's own settings, so a restore can rebuild the folder as it was (the API key is not in it).
CONFIG_DIRNAME = "config"

# Their own folder: retention keeps dozens, and at the top level they buried the three directories
# holding what someone restoring came for.
SNAPSHOT_DIRNAME = "snapshot"

# Recent history in full, older history thinned: everything from the last few hours, then hourly
# for two days, daily for a month, weekly for half a year.
KEEP_ALL_WITHIN_HOURS = 6
KEEP_HOURLY_WITHIN_HOURS = 48
KEEP_DAILY_WITHIN_DAYS = 30
KEEP_WEEKLY_WITHIN_WEEKS = 26


# bucket/<prefix>/<keys as in the bucket>, exports/<workflow>/<folder_id>/...,
# snapshot/extraction-<timestamp>.db, last-status.json.
BUCKET_DIRNAME = "bucket"
# Copied: what cannot be regenerated (site/ is the branding). derived/ is a cache and is not.
BACKED_UP_PREFIXES = (storage.RECORDS_PREFIX, storage.DOCUMENTS_PREFIX, storage.UPLOADS_PREFIX,
                      site_assets.PREFIX)
EXPORTS_DIRNAME = export_archive.BACKUP_DIRNAME


class DestinationUnavailable(RuntimeError):
    """The backup destination cannot be written to right now -- most often a drive that is not
    mounted."""


def check_destination(backup_dir: Path, db_path: str | Path | None = None) -> list[str]:
    """Warnings worth surfacing, or DestinationUnavailable if the destination is unusable."""
    backup_dir = Path(backup_dir)
    root = Path(backup_dir.anchor) if backup_dir.anchor else None
    if root is not None and not root.exists():
        raise DestinationUnavailable(
            f"the backup destination {backup_dir} is not available -- its drive ({root}) is not "
            f"mounted. Reconnect it, or point INCIPIT_BACKUP_DIR somewhere that exists. Nothing was "
            f"backed up."
        )

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        probe = backup_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise DestinationUnavailable(
            f"the backup destination {backup_dir} exists but cannot be written to: {exc}"
        ) from exc

    warnings = []
    if db_path is not None:
        data_anchor = Path(db_path).resolve().anchor.lower()
        dest_anchor = backup_dir.resolve().anchor.lower()
        if data_anchor and data_anchor == dest_anchor:
            warnings.append(
                f"backups are on the same volume as the database ({dest_anchor}) -- this protects "
                f"against corruption and bad deletes, but NOT against that drive failing"
            )
    return warnings


def snapshot_name(moment: datetime) -> str:
    return f"{SNAPSHOT_PREFIX}{moment.strftime(TIMESTAMP_FORMAT)}{SNAPSHOT_SUFFIX}"


def parse_snapshot_time(path: Path) -> datetime | None:
    """The timestamp encoded in a snapshot's filename, or None if it isn't one of ours."""
    name = path.name
    if not (name.startswith(SNAPSHOT_PREFIX) and name.endswith(SNAPSHOT_SUFFIX)):
        return None
    stamp = name[len(SNAPSHOT_PREFIX): -len(SNAPSHOT_SUFFIX)]
    try:
        return datetime.strptime(stamp, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_snapshots(backup_dir: Path) -> list[tuple[datetime, Path]]:
    """Every valid snapshot, newest first."""
    backup_dir = Path(backup_dir)
    found = []
    for directory in (backup_dir / SNAPSHOT_DIRNAME, backup_dir):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            moment = parse_snapshot_time(path)
            if moment is not None:
                found.append((moment, path))
    return sorted(found, reverse=True)


def migrate_snapshots(backup_dir: Path) -> int:
    """Moves any snapshot still at the backup root into `snapshot/`, returning how many."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return 0
    destination = backup_dir / SNAPSHOT_DIRNAME
    moved = 0
    for path in list(backup_dir.iterdir()):
        if parse_snapshot_time(path) is None:
            continue
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / path.name
            if target.exists():
                path.unlink()
            else:
                path.replace(target)
            moved += 1
        except OSError as exc:
            logger.warning("could not move %s into %s (%s)", path.name, SNAPSHOT_DIRNAME, exc)
    if moved:
        logger.info("moved %d snapshot(s) into %s/", moved, SNAPSHOT_DIRNAME)
    return moved


def digest_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a large database does not have to fit in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def has_changed_since_last_snapshot(candidate: Path, backup_dir: Path) -> bool:
    """Is this snapshot different from the newest one already kept?"""
    kept = list_snapshots(backup_dir)
    if not kept:
        return True
    try:
        return digest_of(candidate) != digest_of(kept[0][1])
    except OSError as exc:
        # An unreadable previous snapshot is not a reason to skip taking this one.
        logger.warning("could not compare against %s (%s); keeping the new snapshot",
                       kept[0][1].name, exc)
        return True


def verify_snapshot(path: Path) -> tuple[bool, str]:
    """Opens a snapshot and checks it is a usable database, returning (ok, detail)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"could not open: {exc}"
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            return False, f"integrity_check said {result!r}"
        counts = {}
        for table in ("new_documents", "new_batches", "deletion_log"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                return False, f"table {table} unreadable: {exc}"
        return True, ", ".join(f"{name}={n}" for name, n in counts.items())
    except sqlite3.Error as exc:
        return False, f"verification failed: {exc}"
    finally:
        conn.close()


def _discard_sidecars(partial: Path) -> None:
    """Removes the -wal and -shm files SQLite leaves beside the temporary snapshot."""
    for suffix in ("-wal", "-shm"):
        Path(str(partial) + suffix).unlink(missing_ok=True)


def snapshot_database(db_path: str | Path, backup_dir: Path, moment: datetime | None = None,
                      only_if_changed: bool = False) -> Path | None:
    """One verified snapshot, or None if `only_if_changed` and nothing has changed since the last."""
    backup_dir = Path(backup_dir)
    snapshot_dir = backup_dir / SNAPSHOT_DIRNAME
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    moment = moment or datetime.now(timezone.utc)

    # Written under a temporary name first: a snapshot only takes its real (sortable, discoverable)
    # name once it is complete and verified, so an interrupted run leaves an obviously-partial file
    # rather than something the restore path would happily pick as newest.
    final = snapshot_dir / _free_snapshot_name(backup_dir, moment)
    partial = final.with_suffix(".partial")
    partial.unlink(missing_ok=True)

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    ok, detail = verify_snapshot(partial)
    if not ok:
        partial.unlink(missing_ok=True)
        _discard_sidecars(partial)
        raise RuntimeError(f"snapshot of {db_path} failed verification and was discarded -- {detail}")

    # Verified first, then compared: a snapshot that would not open is a real failure and has to be
    # raised, not quietly dropped as "unchanged".
    if only_if_changed and not has_changed_since_last_snapshot(partial, backup_dir):
        partial.unlink(missing_ok=True)
        _discard_sidecars(partial)
        logger.debug("database unchanged since %s; no snapshot written",
                     list_snapshots(backup_dir)[0][1].name)
        return None

    partial.replace(final)
    _discard_sidecars(partial)
    logger.info("wrote %s (%s)", final.name, detail)
    return final


def _free_snapshot_name(backup_dir: Path, moment: datetime) -> str:
    """A snapshot name not already taken, walking forward a second at a time."""
    taken = {path.name for _, path in list_snapshots(backup_dir)}
    while snapshot_name(moment) in taken:
        moment += timedelta(seconds=1)
    return snapshot_name(moment)


def plan_retention(
    snapshots: list[tuple[datetime, Path]], now: datetime | None = None
) -> tuple[list[Path], list[Path]]:
    """Splits snapshots into (keep, delete) under the thinning policy above."""
    now = now or datetime.now(timezone.utc)
    keep: set[Path] = set()
    seen_hour: set[tuple] = set()
    seen_day: set[tuple] = set()
    seen_week: set[tuple] = set()

    for moment, path in sorted(snapshots, reverse=True):
        age = now - moment
        if age <= timedelta(hours=KEEP_ALL_WITHIN_HOURS):
            keep.add(path)
            continue
        if age <= timedelta(hours=KEEP_HOURLY_WITHIN_HOURS):
            bucket = (moment.year, moment.month, moment.day, moment.hour)
            if bucket not in seen_hour:
                seen_hour.add(bucket)
                keep.add(path)
            continue
        if age <= timedelta(days=KEEP_DAILY_WITHIN_DAYS):
            bucket = (moment.year, moment.month, moment.day)
            if bucket not in seen_day:
                seen_day.add(bucket)
                keep.add(path)
            continue
        if age <= timedelta(weeks=KEEP_WEEKLY_WITHIN_WEEKS):
            bucket = moment.isocalendar()[:2]
            if bucket not in seen_week:
                seen_week.add(bucket)
                keep.add(path)
            continue

    ordered = [path for _, path in sorted(snapshots, reverse=True)]
    return [p for p in ordered if p in keep], [p for p in ordered if p not in keep]


def _sweep_orphaned_partials(backup_dir: Path) -> int:
    """Clears `.partial-wal` / `.partial-shm` files left by older versions, and any `.partial`
    whose snapshot completed."""
    if not backup_dir.is_dir():
        return 0
    removed = 0
    for path in list(backup_dir.iterdir()):
        name = path.name
        if name.endswith((".partial-wal", ".partial-shm")):
            base = backup_dir / name.rsplit(".partial", 1)[0]
        elif name.endswith(".partial"):
            base = backup_dir / name[: -len(".partial")]
        else:
            continue
        if base.with_suffix(".db").exists() or name.endswith((".partial-wal", ".partial-shm")):
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("removed %d leftover partial file(s) from %s", removed, backup_dir)
    return removed


def manifest_path(snapshot: Path) -> Path:
    """Where a snapshot's manifest lives: beside it, under the same name."""
    return snapshot.with_name(snapshot.stem + MANIFEST_SUFFIX)


def referenced_keys(snapshot: Path) -> list[dict]:
    """Every bucket key the snapshot's database refers to, with the document and folder it belongs to."""
    conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    entries: list[dict] = []

    def rows(query: str):
        try:
            return conn.execute(query).fetchall()
        except sqlite3.Error:
            return []
    try:
        for row in rows("SELECT p.bucket_key, d.document_id, d.display_id, d.folder_id FROM new_document_pages p "
                        "JOIN new_documents d ON d.document_id = p.document_id WHERE p.bucket_key IS NOT NULL"):
            entries.append({"key": row["bucket_key"], "kind": "page", "document_id": row["document_id"],
                            "display_id": row["display_id"], "folder_id": row["folder_id"]})
        for row in rows("SELECT document_id, display_id, folder_id FROM new_documents "
                        "WHERE status = 'drafted' AND folder_id IS NOT NULL"):
            entries.append({"key": f"{storage.RECORDS_PREFIX}/{row['folder_id']}/{row['document_id']}.json",
                            "kind": "record", "document_id": row["document_id"], "display_id": row["display_id"],
                            "folder_id": row["folder_id"]})
        for row in rows("SELECT i.bucket_key, i.batch_id FROM new_batch_images i JOIN new_batches b "
                        "ON b.batch_id = i.batch_id WHERE i.bucket_key IS NOT NULL"):
            entries.append({"key": row["bucket_key"], "kind": "upload", "batch_id": row["batch_id"]})
        for row in rows("SELECT bucket_key FROM site_assets"):
            entries.append({"key": row["bucket_key"], "kind": "site asset"})
    finally:
        conn.close()
    return entries


def write_manifest(snapshot: Path, data_dir: Path) -> Path:
    """What the snapshot needs from the bucket, with each file's size as the live bucket holds it now."""
    bucket = Path(data_dir) / "bucket"
    entries = referenced_keys(snapshot)
    for entry in entries:
        path = bucket / entry["key"]
        entry["size"] = path.stat().st_size if path.is_file() else None
    path = manifest_path(snapshot)
    path.write_text(json.dumps({"snapshot": snapshot.name, "written_at": datetime.now(timezone.utc).isoformat(),
                                "files": entries}, indent=1), encoding="utf-8")
    return path


def copy_config(data_dir: Path, backup_dir: Path) -> None:
    config = Path(data_dir) / "incipit.toml"
    if config.is_file():
        target = Path(backup_dir) / CONFIG_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, target / config.name)


def prune(backup_dir: Path, now: datetime | None = None) -> list[Path]:
    """Applies the retention policy, returning what was deleted."""
    _sweep_orphaned_partials(backup_dir)
    snapshots = list_snapshots(backup_dir)
    if len(snapshots) <= 1:
        return []
    _, doomed = plan_retention(snapshots, now=now)
    newest = snapshots[0][1]
    deleted = []
    for path in doomed:
        if path == newest:
            continue
        path.unlink(missing_ok=True)
        manifest_path(path).unlink(missing_ok=True)
        deleted.append(path)
    if deleted:
        logger.info("pruned %d old snapshot(s)", len(deleted))
    return deleted


def copy_tree_additive(source: Path, destination: Path) -> int:
    """Copies files that aren't already at the destination, returning how many were copied."""
    if not source.is_dir():
        return 0
    copied = 0
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        target = destination / item.relative_to(source)
        if target.exists() and not _differs(item, target):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied += 1
    return copied


# Two seconds, because the offsite copy is an SMB share and timestamps there do not always survive
# at the resolution the local disk records them.
MTIME_TOLERANCE_SECONDS = 2

# Files at or under this are read and compared byte for byte when the cheap tests are inconclusive.
DIGEST_MAX_BYTES = 1 << 20


def _differs(source: Path, target: Path) -> bool:
    """Whether `source` has changed since it was copied to `target`."""
    try:
        a, b = source.stat(), target.stat()
    except OSError:
        return True
    if a.st_size != b.st_size:
        return True
    if a.st_mtime - b.st_mtime > MTIME_TOLERANCE_SECONDS:
        return True
    if a.st_size > DIGEST_MAX_BYTES:
        return False
    try:
        return source.read_bytes() != target.read_bytes()
    except OSError:
        return True

















def copy_bucket(data_dir: Path, backup_dir: Path) -> int:
    """Every record, scan and upload the bucket holds, at the same key under bucket/, copying only
    what is new or has changed. Records are rewritten in place as reviewers save."""
    copied = 0
    for prefix in BACKED_UP_PREFIXES:
        copied += copy_tree_additive(Path(data_dir) / "bucket" / prefix,
                                     Path(backup_dir) / BUCKET_DIRNAME / prefix)
    return copied




def _live(conn) -> tuple[set[str], set[str], set[str]]:
    """(document ids, folder ids, batch ids) the database still has -- what a deletion must not touch."""
    def ids(query: str) -> set[str]:
        try:
            return {row[0] for row in conn.execute(query) if row[0]}
        except sqlite3.Error:
            return set()
    return (ids("SELECT document_id FROM new_documents"), ids("SELECT folder_id FROM folders"),
            ids("SELECT batch_id FROM new_batches"))


def apply_deletions(db_path: str | Path, backup_dir: Path) -> int:
    """Removes from a backup what the site recorded as deleted, and only that.

    The log names internal ids, and the bucket's keys are built from them, so each entry maps to
    exact paths: a document is `documents/*/<id>/` and `records/*/<id>.json`, a folder is
    `documents/<id>/` and `records/<id>/`, a pending batch is `uploads/<id>/`. Anything the
    database still has is left alone, so a folder replaced under the same id keeps its new copy.
    """
    root = Path(backup_dir) / BUCKET_DIRNAME
    if not root.is_dir():
        return 0
    try:
        with db.connect(str(db_path)) as conn:
            deleted = conn.execute(
                "SELECT target_kind, target_id, details_json FROM deletion_log").fetchall()
            live_documents, live_folders, live_batches = _live(conn)
    except Exception as exc:  # noqa: BLE001 - a backup is not worth failing over the log
        logger.warning("could not read the deletion log (%s); nothing was removed", exc)
        return 0

    documents, records = root / storage.DOCUMENTS_PREFIX, root / storage.RECORDS_PREFIX
    uploads = root / storage.UPLOADS_PREFIX
    doomed: list[Path] = []

    def document(document_id: str) -> None:
        if not document_id or document_id in live_documents:
            return
        doomed.extend(documents.glob(f"*/{document_id}"))
        doomed.extend(records.glob(f"*/{document_id}.json"))

    for row in deleted:
        kind, target = row["target_kind"], (row["target_id"] or "").strip()
        if kind == "new_document":
            document(target)
        elif kind == "new_folder":
            for document_id in _logged(row, "document_ids"):
                document(document_id)
            if target and target not in live_folders:
                doomed.extend([documents / target, records / target])
            for batch_id in _logged(row, "batch_ids"):
                if batch_id not in live_batches:
                    doomed.append(uploads / batch_id)
        elif kind == "pending_batch":
            if target and target not in live_batches:
                doomed.append(uploads / target)

    gone = 0
    for path in dict.fromkeys(doomed):
        try:
            if path.is_dir():
                shutil.rmtree(path)
                gone += 1
            elif path.is_file():
                path.unlink()
                gone += 1
        except OSError as exc:
            logger.warning("could not remove %s from the backup (%s)", path, exc)
    if gone:
        logger.info("removed %d deleted item(s) from %s", gone, backup_dir)
    return gone


def write_status(
    backup_dir: Path, ok: bool, detail: str, snapshot: Path | None = None,
    also: Path | None = None,
) -> Path | None:
    """Records the outcome where something else can notice it."""
    previous = read_status(backup_dir) or (read_status(also) if also else None)
    payload = {
        "ok": ok,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot.name if snapshot else None,
        # Kept across failures so a run of failures still shows when protection was last real.
        "last_success_at": (
            datetime.now(timezone.utc).isoformat() if ok else (previous or {}).get("last_success_at")
        ),
    }
    # Written to the destination when it is reachable, and always to `also` (a local directory) so
    # the record survives the very failure it most needs to report.
    body = json.dumps(payload, indent=2)
    written = None
    for directory in (backup_dir, also):
        if directory is None:
            continue
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / STATUS_FILENAME
            path.write_text(body, encoding="utf-8")
            written = written or path
        except OSError:
            # Never raise from here: this runs inside the except block of a failing backup, and an
            # exception thrown while recording a failure masks the failure itself.
            continue
    return written


def read_status(backup_dir: Path | None) -> dict | None:
    if backup_dir is None:
        return None
    try:
        path = Path(backup_dir) / STATUS_FILENAME
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        # An unreachable drive must read as "no status", not as an exception -- callers use this to
        # report trouble, so it cannot itself be a source of trouble.
        return None












def _logged(row, field: str) -> list[str]:
    """One list a folder-level deletion entry recorded, if it recorded any."""
    try:
        details = json.loads(row["details_json"] or "{}")
    except ValueError:
        return []
    listed = details.get(field) if isinstance(details, dict) else None
    return [d for d in listed if isinstance(d, str)] if isinstance(listed, list) else []






def mirror_to(backup_dir: Path, mirror_dir: str | Path | None) -> dict:
    """Copies the local backup set onto a second destination, best effort."""
    if not mirror_dir:
        return {"state": "not configured"}
    mirror_dir = Path(mirror_dir)
    try:
        check_destination(mirror_dir)
        # Exports use deterministic names and may be replaced or renamed. Reconcile this subtree
        # before the additive backup copy so obsolete deliverables do not persist in the mirror.
        exports_copied = export_archive.sync_tree(
            backup_dir / EXPORTS_DIRNAME, mirror_dir / EXPORTS_DIRNAME)
        copied = exports_copied + copy_tree_additive(backup_dir, mirror_dir)
        return {"state": "ok", "path": str(mirror_dir), "files_copied": copied}
    except Exception as exc:  # noqa: BLE001 - a mirror failure must never fail the backup
        logger.warning("offsite mirror to %s unavailable: %s", mirror_dir, exc)
        return {"state": "unavailable", "path": str(mirror_dir), "detail": str(exc)}


def run_backup(
    db_path: str | Path,
    backup_dir: str | Path,
    data_dir: str | Path | None = None,
    status_dir: str | Path | None = None,
    mirror_dir: str | Path | None = None,
    only_if_changed: bool = False,
    snapshot_database_too: bool = True,
    export_dir: str | Path | None = None,
) -> dict:
    """One full pass: snapshot, verify, prune, copy the cold data, mirror. One pass at a time: the
    timer, "Back up now" and the tray (another process) wait for each other on a lock file."""
    backup_dir = Path(backup_dir)
    lock = Path(status_dir or Path(db_path).parent) / ".backup.lock"
    with file_locks.hold(lock, wait=True):
        return _run_backup(db_path, backup_dir, data_dir, status_dir, mirror_dir, only_if_changed,
                           snapshot_database_too, export_dir)


def _run_backup(db_path, backup_dir: Path, data_dir, status_dir, mirror_dir, only_if_changed: bool,
                snapshot_database_too: bool, export_dir) -> dict:
    try:
        # Before anything else: is the destination actually there?
        for warning in check_destination(backup_dir, db_path):
            logger.warning("%s", warning)
        migrate_snapshots(backup_dir)
        # A pass that only syncs files: the records and scans reach the drive on the fast cadence
        # while snapshots, which accumulate at 3MB each, stay on their own.
        snapshot = (snapshot_database(db_path, backup_dir, only_if_changed=only_if_changed)
                    if snapshot_database_too else None)
        # Nothing to thin when nothing was added, and pruning on an unchanged tick would age the
        # history on wall-clock time alone -- at a ten-minute cadence that quietly deletes real
        # states during a quiet week.
        pruned = prune(backup_dir) if snapshot else []

        copied = 0
        exports_copied = 0
        if data_dir is not None:
            copied = copy_bucket(Path(data_dir), backup_dir)
            copy_config(Path(data_dir), backup_dir)
            # After the bucket copy, so every file the manifest names is already in the backup.
            if snapshot is not None:
                write_manifest(snapshot, Path(data_dir))
        if export_dir is not None or data_dir is not None:
            source_exports = export_archive.source_root(data_dir or Path(db_path).parent, export_dir)
            exports_copied = export_archive.sync_tree(source_exports, backup_dir / EXPORTS_DIRNAME)
        # Before the mirror, so the removals travel with everything else rather than a run later.
        deletions_applied = apply_deletions(db_path, backup_dir)
        # After the local snapshot is safely written and verified, never before: the offsite copy
        # is a copy *of* a backup, and attempting it first would let an unreachable share decide
        # whether a backup happens at all.
        mirror = mirror_to(backup_dir, mirror_dir)
        if mirror_dir and mirror["state"] != "unavailable":
            deletions_applied += apply_deletions(db_path, Path(mirror_dir))

        summary = {
            "deletions_applied": deletions_applied,
            "snapshot": snapshot.name if snapshot else None,
            "unchanged": snapshot is None,
            "bytes": snapshot.stat().st_size if snapshot else 0,
            "pruned": len(pruned),
            "files_copied": copied,
            "exports_copied": exports_copied,
            "snapshots_kept": len(list_snapshots(backup_dir)),
            "mirror": mirror,
        }
        detail = (f"{summary['snapshots_kept']} snapshot(s) kept" if snapshot else
                  f"no change since the last snapshot; {summary['snapshots_kept']} kept")
        if mirror["state"] == "unavailable":
            # Said in the status file rather than only the log, because this is the failure that
            # goes unnoticed: the backups are fine, and the offsite copy quietly stops ageing.
            detail += f"; offsite mirror unreachable ({mirror['path']})"
        write_status(backup_dir, True, detail, snapshot, also=status_dir)
        return summary
    except Exception as exc:
        write_status(backup_dir, False, str(exc), also=status_dir)
        raise


# ---------- the in-server timer ----------
# Replaces a scheduled task triggered `-AtLogOn`, which never fired with nobody logged on and
# failed the way it succeeded: silently.

# How often the reviewer's work reaches the drive.
BACKUP_INTERVAL_MINUTES = 2

# How often the database itself is snapshotted.
SNAPSHOT_INTERVAL_MINUTES = 10

# Long enough that a restart loop cannot turn into a backup loop, short enough that a server
# started for the day protects that day's first hour of work.
FIRST_RUN_DELAY_SECONDS = 60


def backup_forever(
    db_path: str | Path,
    backup_dir: str | Path,
    data_dir: str | Path | None = None,
    status_dir: str | Path | None = None,
    mirror_dir: str | Path | None = None,
    interval_minutes: float = BACKUP_INTERVAL_MINUTES,
    stop: threading.Event | None = None,
    first_delay_seconds: float = FIRST_RUN_DELAY_SECONDS,
    snapshot_interval_minutes: float = SNAPSHOT_INTERVAL_MINUTES,
    clock=time.monotonic,
    export_dir: str | Path | None = None,
) -> None:
    """Syncs the files every `interval_minutes` and snapshots the database every
    `snapshot_interval_minutes`, skipping the snapshot when nothing has changed."""
    stop = stop or threading.Event()
    if stop.wait(first_delay_seconds):
        return
    # None, not 0.0: time.monotonic() counts from boot, so on a machine that started minutes ago a
    # zero would read as "snapshotted just now" and the first snapshot would wait out the whole
    # cadence.
    last_snapshot = None
    while True:
        try:
            due = (last_snapshot is None
                   or (clock() - last_snapshot) >= snapshot_interval_minutes * 60)
            summary = run_backup(
                db_path, backup_dir, data_dir=data_dir, status_dir=status_dir,
                mirror_dir=mirror_dir, only_if_changed=True, snapshot_database_too=due,
                **({"export_dir": export_dir} if export_dir is not None else {}),
            )
            if due:
                last_snapshot = clock()
            if summary.get("unchanged"):
                logger.debug("backup tick: no change since %s", summary.get("snapshot"))
            else:
                logger.info("backup: wrote %s (%d kept)",
                            summary["snapshot"], summary["snapshots_kept"])
        except Exception as exc:  # noqa: BLE001 - see docstring: never fatal to the server
            logger.warning("scheduled backup failed: %s", exc)
        if stop.wait(interval_minutes * 60):
            return


def start_backup_timer(
    db_path: str | Path,
    backup_dir: str | Path,
    data_dir: str | Path | None = None,
    status_dir: str | Path | None = None,
    mirror_dir: str | Path | None = None,
    interval_minutes: float = BACKUP_INTERVAL_MINUTES,
    snapshot_interval_minutes: float = SNAPSHOT_INTERVAL_MINUTES,
    export_dir: str | Path | None = None,
) -> threading.Event:
    """Starts backup_forever on a daemon thread and returns the event that stops it."""
    stop = threading.Event()
    thread = threading.Thread(
        target=backup_forever,
        kwargs=dict(db_path=db_path, backup_dir=backup_dir, data_dir=data_dir,
                    status_dir=status_dir, mirror_dir=mirror_dir,
                    interval_minutes=interval_minutes, stop=stop,
                    snapshot_interval_minutes=snapshot_interval_minutes,
                    export_dir=export_dir),
        name="incipit-backup", daemon=True,
    )
    thread.start()
    logger.info("backup timer started: files every %g minutes, database every %g, into %s",
                interval_minutes, snapshot_interval_minutes, backup_dir)
    return stop
