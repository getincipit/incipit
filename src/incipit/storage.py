"""Bucket storage abstraction backed by fsspec."""

from __future__ import annotations

import logging
import os
import posixpath
import urllib.parse
import time
import uuid

import fsspec
from fsspec.implementations.local import LocalFileSystem

logger = logging.getLogger(__name__)

# Where reduced copies of a scan live, mirroring the original's own path one level down:
# derived/{size}/{original key}.
DERIVED_PREFIX = "derived"

# The bucket's top-level folders, named for what they hold. Every key below them is built from
# internal ids (documents/{folder_id}/{document_id}/..., records/{folder_id}/{document_id}.json,
# uploads/{batch_id}/...), so renaming anything never moves a file.
DOCUMENTS_PREFIX = "documents"
UPLOADS_PREFIX = "uploads"
RECORDS_PREFIX = "records"


def document_dir(prefix: str, folder_id: str, document_id: str) -> str:
    """{prefix}/{folder_id}/{document_id} -- where everything about one document lives."""
    if not folder_id or not document_id:
        raise ValueError("a document's files need both its folder id and its document id")
    return posixpath.join(prefix, folder_id, document_id)


def derived_key(key: str, size: str) -> str:
    """The bucket key holding `key` rendered at `size`."""
    return posixpath.join(DERIVED_PREFIX, size, key)


# Windows refuses to replace a file another reader holds open (a backup copy, an export stream) for those few milliseconds.
REPLACE_ATTEMPTS = 6


def _replace(source: str, destination: str) -> None:
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


class BucketStorage:
    def __init__(self, bucket_uri: str):
        self.bucket_uri = bucket_uri.rstrip("/")
        self.fs, self._root = fsspec.core.url_to_fs(self.bucket_uri)
        # A file:// address may be percent-encoded (a space as %20); the folder on disk is not.
        if self.bucket_uri.startswith("file://") and "%" in self._root:
            self._root = urllib.parse.unquote(self._root)

    def _full_path(self, key: str) -> str:
        # Keys are built from internal ids and sanitised filenames; this is the last line of
        # defence against a key that would escape the bucket, not the first.
        full_path = posixpath.normpath(posixpath.join(self._root, key))
        root = posixpath.normpath(self._root)
        if full_path != root and not full_path.startswith(root + "/"):
            raise ValueError(f"refusing to build a bucket path outside the bucket root: {key!r}")
        return full_path

    def upload_bytes(
        self, path: str, filename: str, content: bytes, prefix: str = UPLOADS_PREFIX
    ) -> tuple[str, str]:
        """Store bytes under {prefix}/{path}/{filename}, returning (bucket_key, bucket_uri)."""
        key = posixpath.join(prefix, path, filename)
        self.write_bytes(key, content)
        return key, f"{self.bucket_uri}/{key}"

    def exists(self, key: str) -> bool:
        return self.fs.exists(self._full_path(key))

    def list_keys(self, prefix: str) -> set[str]:
        """Every stored key under `prefix`, in one listing rather than one lookup per key."""
        root = self._full_path(prefix)
        if not self.fs.exists(root):
            return set()
        base = posixpath.normpath(self._root)
        return {posixpath.relpath(path.replace("\\", "/"), base) for path in self.fs.find(root)}

    def size(self, key: str) -> int:
        """Byte size of a stored object without reading its content -- used to budget how many
        images fit in one split-proposal window by request size, not just page count (see
        split.py's MAX_SPLIT_WINDOW_BYTES)."""
        return self.fs.info(self._full_path(key))["size"]

    def read_bytes(self, key: str) -> bytes:
        with self.fs.open(self._full_path(key), "rb") as fh:
            return fh.read()

    def open_stream(self, key: str):
        """A context-managed reader for exports too large to assemble in memory."""
        return self.fs.open(self._full_path(key), "rb")

    def write_bytes(self, key: str, content: bytes) -> None:
        """Store bytes at an exact key, rather than at the {prefix}/{document_id}/{filename} shape
        upload_bytes builds."""
        full_path = self._full_path(key)
        self.fs.makedirs(posixpath.dirname(full_path), exist_ok=True)
        # Write beside the destination, then replace it only after a complete write. A failed
        # merge/re-split must never truncate the live scan that the database still references.
        temporary = f"{full_path}.tmp-{uuid.uuid4().hex}"
        try:
            with self.fs.open(temporary, "wb") as fh:
                fh.write(content)
                if isinstance(self.fs, LocalFileSystem):
                    fh.flush()
                    os.fsync(fh.fileno())
            if isinstance(self.fs, LocalFileSystem):
                _replace(temporary, full_path)
            else:
                self.fs.mv(temporary, full_path)
        finally:
            if self.fs.exists(temporary):
                self.fs.rm(temporary)

    def upload_document(self, folder_id: str, document_id: str, filename: str,
                        content: bytes) -> tuple[str, str]:
        """Store one of a document's own scans: documents/{folder_id}/{document_id}/{filename}."""
        document_dir(DOCUMENTS_PREFIX, folder_id, document_id)
        return self.upload_bytes(posixpath.join(folder_id, document_id), filename, content,
                                 prefix=DOCUMENTS_PREFIX)

    # ---------- a document's JSON record ----------

    def record_key(self, folder_id: str, document_id: str) -> str:
        """Where a document's record lives: records/{folder_id}/{document_id}.json."""
        if not folder_id or not document_id:
            raise ValueError("a record needs both its folder id and its document id")
        return posixpath.join(RECORDS_PREFIX, folder_id, f"{document_id}.json")

    def write_record(self, folder_id: str, document_id: str, text: str) -> str:
        key = self.record_key(folder_id, document_id)
        self.write_bytes(key, text.encode("utf-8"))
        return key

    def read_record(self, folder_id: str, document_id: str) -> str | None:
        key = self.record_key(folder_id, document_id)
        return self.read_bytes(key).decode("utf-8") if self.exists(key) else None

    def copy(self, source_key: str, folder_id: str, document_id: str,
             filename: str) -> tuple[str, str]:
        """Copies an already-stored scan into a document's own directory -- used when a split
        batch's raw intake images are finalized into documents, keeping the intake batch as an
        audit trail rather than moving it."""
        key, uri = self.upload_document(folder_id, document_id, filename,
                                        self.read_bytes(source_key))
        self._copy_derived(source_key, key)
        return key, uri

    def _copy_derived(self, source_key: str, target_key: str) -> None:
        """Carry every already-rendered size of `source_key` over to `target_key`."""
        for size in self._derived_sizes():
            source = self._full_path(derived_key(source_key, size))
            if not self.fs.exists(source):
                continue
            try:
                target = self._full_path(derived_key(target_key, size))
                self.fs.makedirs(posixpath.dirname(target), exist_ok=True)
                self.fs.copy(source, target)
            except (OSError, ValueError) as exc:
                logger.warning("could not carry the %s copy of %s across to %s (%s)",
                               size, source_key, target_key, exc)

    def _derived_sizes(self) -> list[str]:
        """The sizes the bucket actually holds renderings for."""
        root = self._full_path(DERIVED_PREFIX)
        if not self.fs.exists(root):
            return []
        try:
            return [name for name in
                    (posixpath.basename(p.rstrip("/")) for p in self.fs.ls(root, detail=False))
                    if name]
        except (FileNotFoundError, OSError):
            return []

    def delete_document(self, folder_id: str, document_id: str) -> None:
        """Every scan stored for one document, and their cached renderings."""
        self.delete_prefix(DOCUMENTS_PREFIX, posixpath.join(folder_id, document_id))

    def delete_record(self, folder_id: str, document_id: str) -> None:
        """A document's record."""
        path = self._full_path(self.record_key(folder_id, document_id))
        if self.fs.exists(path):
            self.fs.rm(path)

    def delete_keys(self, keys) -> None:
        """Named objects and their cached renderings, where the directory holding them is shared."""
        sizes = self._derived_sizes()
        for key in keys:
            if not key:
                continue
            for path in [key, *(derived_key(key, size) for size in sizes)]:
                full_path = self._full_path(path)
                if self.fs.exists(full_path):
                    self.fs.rm(full_path)

    def delete_prefix(self, prefix: str, path: str) -> None:
        """Deletes every object under {prefix}/{path}/ and its cached renderings -- a document's
        scans, a folder's, or a pending batch's raw uploads."""
        for path in [posixpath.join(prefix, path), *self._derived_mirrors(prefix, path)]:
            full_path = self._full_path(path)
            if self.fs.exists(full_path):
                self.fs.rm(full_path, recursive=True)

    def _derived_mirrors(self, prefix: str, path: str) -> list[str]:
        """Where each size's copy of {prefix}/{path}/ lives."""
        return [posixpath.join(DERIVED_PREFIX, size, prefix, path)
                for size in self._derived_sizes()]

    def delete_all(self) -> None:
        """Wipes every object under the bucket root, for the dev "start fresh" reset script."""
        root = self._full_path("")
        if self.fs.exists(root):
            self.fs.rm(root, recursive=True)
        self.fs.makedirs(root, exist_ok=True)
