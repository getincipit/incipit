"""Canonical paths and durable replication for folder export deliverables.

A folder's exports live in `{root}/{workflow}/{folder_id}/`, the folder's permanent internal id, so a
rename never moves them; the files inside are named for the folder's display title.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "exports"
WORKFLOWS = {"new"}
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_CHUNK = 1024 * 1024
TREE_LOCK = threading.RLock()


def source_root(data_dir: str | Path, configured: str | Path | None = None) -> Path:
    """One resolver for server, backup and maintenance tools; prefer canonical storage over legacy."""
    if configured is not None:
        return Path(configured)
    data = Path(data_dir)
    return data / ("exports" if (data / "exports").is_dir() else "export")


def publish_directory(root, group, folder_id, title, stage):
    """Publish one complete generation, retaining the previous generation on rename failure.

    The caller holds TREE_LOCK, also used by backup traversal. Staging is on the same volume.
    """
    target = Path(root) / relative_path(group, folder_id, title).parent
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = Path(stage).parent / "previous"
    if target.exists():
        target.rename(previous)
    try:
        Path(stage).rename(target)
    except BaseException:
        if previous.exists():
            previous.rename(target)
        raise
    if previous.exists():
        shutil.rmtree(previous)
    return target


def migrate_legacy(root):
    """Copy old data/export archives once; never delete or overwrite either installation's files."""
    root = Path(root)
    legacy = root.parent / "export"
    if root.name != "exports" or not legacy.is_dir():
        return
    with TREE_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".legacy-imported"
        if marker.exists():
            return
        for workflow in WORKFLOWS:
            for source in (legacy / workflow).rglob("*"):
                if source.is_file():
                    target = root / source.relative_to(legacy)
                    if not target.exists():
                        _atomic_copy(source, target)
        marker.touch()


def folder_directory(folder_id: str) -> str:
    """The directory holding one folder's exports: its internal id, which never changes."""
    if not folder_id or not _VALID_ID.fullmatch(folder_id):
        raise ValueError(f"not a folder id: {folder_id!r}")
    return folder_id


def basename(title: str) -> str:
    """A filename without an extension, from the folder's display title ("E2002_Advice")."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", title or "")
    safe = re.sub(r"_+", "_", safe).strip("._-") or "folder"
    return f"{safe[:120]}_transcriptions"


_GROUP = re.compile(r"^[a-z0-9-]{2,32}$")


def relative_path(group: str, folder_id: str, title: str) -> Path:
    """`{tab slug}/{folder id}/{title}.zip`: grouped by tab, so same-named folders in two tabs stay apart."""
    if not _GROUP.fullmatch(group or ""):
        raise ValueError(f"not an export group: {group!r}")
    return Path(group) / folder_directory(folder_id) / f"{basename(title)}.zip"


def _atomic_copy(source: Path, path: Path) -> None:
    """Replace one archive with a file, copied in chunks so its size never becomes memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".partial", delete=False) as handle:
            partial = Path(handle.name)
            with open(source, "rb") as reader:
                shutil.copyfileobj(reader, handle, _CHUNK)
            handle.flush()
        partial.replace(path)
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)


def _same_contents(left: Path, right: Path) -> bool:
    """Chunk by chunk: an export archive is far too large to hold two copies of in memory."""
    with open(left, "rb") as a, open(right, "rb") as b:
        while True:
            chunk = a.read(_CHUNK)
            if chunk != b.read(_CHUNK):
                return False
            if not chunk:
                return True


def forget(export_root: str | Path, folder_id: str) -> list[Path]:
    """Remove a folder's export archives, for a folder that is being deleted or replaced.

    The working tree only: it is the authoritative one, and sync_tree carries the removal out to
    the backup and the mirror on the next pass. Without this the deliverable of a folder nobody
    can reach any more keeps being replicated to both. Every group is searched, since a folder's
    id is unique across tabs and an older tree may hold it under another name.
    """
    removed = []
    root = Path(export_root)
    with TREE_LOCK:
        for directory in sorted(root.glob(f"*/{folder_directory(folder_id)}")) if root.is_dir() else []:
            if not directory.is_dir():
                continue
            try:
                shutil.rmtree(directory)
                removed.append(directory)
            except OSError as exc:
                logger.warning("could not remove the export archive %s: %s", directory, exc)
    return removed


def sync_tree(source: str | Path, destination: str | Path) -> int:
    with TREE_LOCK:
        return _sync_tree(source, destination)


def _sync_tree(source: str | Path, destination: str | Path) -> int:
    """Make one export archive tree exactly match another, returning copied file count.

    A missing source means "not configured yet", never "erase the destination". Once the source
    exists, stale paths are removed so renames do not accumulate forever.
    """
    source, destination = Path(source), Path(destination)
    a, b = source.resolve(), destination.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise ValueError("export source and destination must be separate non-overlapping directories")
    if not source.is_dir():
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    files = [path for path in source.rglob("*") if path.is_file()
             and not any(part.startswith(".") for part in path.relative_to(source).parts)]
    expected = {path.relative_to(source) for path in files}
    for target in sorted((p for p in destination.rglob("*") if p.is_file()), reverse=True):
        if target.relative_to(destination) not in expected:
            target.unlink()
    copied = 0
    for item in sorted(files):
        target = destination / item.relative_to(source)
        if target.is_file() and item.stat().st_size == target.stat().st_size:
            if _same_contents(item, target):
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(item, target)
        copied += 1
    for directory in sorted((p for p in destination.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return copied
