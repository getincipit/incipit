"""Crash-released, cross-process locks for filesystem-backed workflow resources.

Lock files are coordination markers, never data. Do not unlink them: a waiter may already
hold the same inode. The OS releases ownership if a worker exits unexpectedly.
"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time


@contextmanager
def hold(path: Path, *, wait: bool = False):
    """Yield whether the caller owns the lock; optionally wait without a SQLite transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if not wait:
                    yield False
                    return
                time.sleep(0.1)
        try:
            yield True
        finally:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
