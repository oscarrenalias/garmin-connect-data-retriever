"""Tests for the single-writer lock."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

import pytest

from garmin_archive.models import ArchiveLockedError
from garmin_archive.storage.lock import writer_lock


def _try_acquire(db_path: str, queue: multiprocessing.Queue[str]) -> None:
    try:
        with writer_lock(Path(db_path)):
            queue.put("acquired")
    except ArchiveLockedError as exc:
        queue.put(exc.code)
    except BaseException as exc:  # pragma: no cover - reported to parent
        queue.put(f"unexpected:{type(exc).__name__}")


def _child_result(db_path: Path) -> str:
    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = ctx.Queue()
    proc = ctx.Process(target=_try_acquire, args=(str(db_path), queue))
    proc.start()
    try:
        result = queue.get(timeout=60)
    finally:
        proc.join(timeout=60)
    return result


def test_child_blocked_while_held_then_succeeds_after_release(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    with writer_lock(db_path):
        assert _child_result(db_path) == "ARCHIVE_LOCKED"
    assert _child_result(db_path) == "acquired"


def test_same_process_second_acquire_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    with writer_lock(db_path), pytest.raises(ArchiveLockedError) as info, writer_lock(db_path):
        pass
    assert info.value.retryable is True


def test_lock_released_when_body_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    with pytest.raises(RuntimeError), writer_lock(db_path):
        raise RuntimeError("boom")
    with writer_lock(db_path):
        pass


def test_failed_acquire_does_not_release_holder(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    with writer_lock(db_path):
        with pytest.raises(ArchiveLockedError), writer_lock(db_path):
            pass
        assert _child_result(db_path) == "ARCHIVE_LOCKED"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_lock_file_mode_is_0600(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    with writer_lock(db_path):
        pass
    lock_file = tmp_path / "archive.db.lock"
    assert lock_file.exists()
    assert lock_file.stat().st_mode & 0o777 == 0o600
