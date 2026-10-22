"""Single-writer lock for the archive database."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from garmin_archive.models import ArchiveLockedError


def _try_lock(fd: int) -> None:
    """Take an exclusive non-blocking lock on ``fd``; raise ``OSError`` if held."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def writer_lock(db_path: Path) -> Iterator[None]:
    """Hold the exclusive writer lock for ``db_path`` for the duration of the block.

    The lock lives on ``<db_path>.lock``; the file may remain on disk after release.
    Raises ``ArchiveLockedError`` if another writer already holds the lock.
    """
    lock_path = db_path.with_name(db_path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _try_lock(fd)
        except OSError as exc:
            raise ArchiveLockedError("Another process holds the archive writer lock.") from exc
        try:
            yield
        finally:
            with suppress(OSError):
                _unlock(fd)
    finally:
        os.close(fd)
