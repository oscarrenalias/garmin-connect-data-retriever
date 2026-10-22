from __future__ import annotations

import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import garmin_archive
from garmin_archive import (
    ArchiveClient,
    ArchiveConfig,
    ArchiveLockedError,
    DatabaseNotFoundError,
    InitResult,
    NotInitializedError,
)
from garmin_archive.storage.lock import writer_lock


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


def _client(path: Path) -> ArchiveClient:
    return ArchiveClient(ArchiveConfig(db_path=path), clock=FixedClock(), environ={})


def test_initialize_new_database(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "archive.db"
    with _client(path) as client:
        result = client.initialize()
    assert isinstance(result, InitResult)
    assert result.db_path == path.resolve()
    assert result.created is True
    assert result.schema_version == 1
    assert result.applied_migrations == [1]
    conn = sqlite3.connect(path)
    try:
        kinds = dict(
            conn.execute(
                "SELECT type, COUNT(*) FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index') GROUP BY type"
            ).fetchall()
        )
        assert kinds == {"table": 9, "index": 7}
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        row = conn.execute("SELECT value_json FROM archive_metadata WHERE key='schema'").fetchone()
        assert '"schema_version": 1' in row[0]
        assert '"package_version"' in row[0]
    finally:
        conn.close()
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_foreign_keys_on_for_write_connection(tmp_path: Path) -> None:
    from garmin_archive.storage.connection import open_write_connection

    _client(tmp_path / "a.db").initialize()
    conn = open_write_connection(tmp_path / "a.db", busy_timeout_ms=100)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_second_initialize_is_noop(tmp_path: Path) -> None:
    client = _client(tmp_path / "archive.db")
    client.initialize()
    second = client.initialize()
    assert second.created is False
    assert second.applied_migrations == []
    assert second.schema_version == 1


def test_initialize_raises_when_locked(tmp_path: Path) -> None:
    path = (tmp_path / "archive.db").resolve()
    with writer_lock(path), pytest.raises(ArchiveLockedError):
        _client(path).initialize()


def test_open_existing_read_missing_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(DatabaseNotFoundError):
        _client(path)._open_existing_read()  # pyright: ignore[reportPrivateUsage]
    assert list(tmp_path.iterdir()) == []


def test_open_existing_read_uninitialized(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(NotInitializedError):
        _client(path)._open_existing_read()  # pyright: ignore[reportPrivateUsage]


def test_open_existing_read_initialized(tmp_path: Path) -> None:
    client = _client(tmp_path / "archive.db")
    client.initialize()
    conn = client._open_existing_read()  # pyright: ignore[reportPrivateUsage]
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM schema_migrations")
    finally:
        conn.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    client = _client(tmp_path / "archive.db")
    client.close()
    client.close()


def test_reexports() -> None:
    for name in garmin_archive.__all__:
        assert hasattr(garmin_archive, name)
