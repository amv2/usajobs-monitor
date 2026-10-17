"""A non-blocking exclusive file lock: fcntl on POSIX, msvcrt on Windows."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockBusy(RuntimeError):
    """Another process holds the lock."""


@contextmanager
def exclusive_lock(path: Path, busy_message: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LockBusy("The lock file is not a regular file.")
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise LockBusy(busy_message) from None
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusy(busy_message) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
