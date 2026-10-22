from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from garmin_archive.models import (
    MigrationChecksumError,
    NotInitializedError,
    SchemaVersionError,
    StorageError,
)
from garmin_archive.storage.migrations import (
    DirectoryMigrations,
    apply_migrations,
    check_schema,
    current_schema_version,
)


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    yield c
    c.close()


def _src(tmp_path: Path, files: dict[str, str]) -> DirectoryMigrations:
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)
    return DirectoryMigrations(tmp_path)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_apply_and_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    src = _src(tmp_path, {"0001_a.sql": "CREATE TABLE t1 (x);"})
    assert apply_migrations(conn, FixedClock(), source=src) == [1]
    assert apply_migrations(conn, FixedClock(), source=src) == []
    assert current_schema_version(conn) == 1
    assert check_schema(conn, source=src) == 1


def test_applied_at_exact(conn: sqlite3.Connection, tmp_path: Path) -> None:
    src = _src(tmp_path, {"0001_a.sql": "CREATE TABLE t1 (x);"})
    apply_migrations(conn, FixedClock(), source=src)
    row = conn.execute("SELECT applied_at_utc FROM schema_migrations").fetchone()
    assert row[0] == "2026-01-02T03:04:05.123456Z"


def test_too_new_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    src = _src(tmp_path, {"0001_a.sql": "CREATE TABLE t1 (x);"})
    apply_migrations(conn, FixedClock(), source=src)
    conn.execute("INSERT INTO schema_migrations VALUES (99, 'x', 'y')")
    with pytest.raises(SchemaVersionError) as info:
        apply_migrations(conn, FixedClock(), source=src)
    assert info.value.details["db_version"] == 99
    assert info.value.details["supported_version"] == 1


def test_checksum_mismatch(conn: sqlite3.Connection, tmp_path: Path) -> None:
    src = _src(tmp_path, {"0001_a.sql": "CREATE TABLE t1 (x);"})
    apply_migrations(conn, FixedClock(), source=src)
    conn.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")
    with pytest.raises(MigrationChecksumError) as info:
        apply_migrations(conn, FixedClock(), source=src)
    assert info.value.details["version"] == 1
    with pytest.raises(MigrationChecksumError):
        check_schema(conn, source=src)


def test_failed_migration_rolls_back(conn: sqlite3.Connection, tmp_path: Path) -> None:
    src = _src(
        tmp_path,
        {
            "0001_a.sql": "CREATE TABLE t1 (x);",
            "0002_b.sql": "CREATE TABLE t2 (x);\nNOT VALID SQL;",
        },
    )
    with pytest.raises(StorageError) as info:
        apply_migrations(conn, FixedClock(), source=src)
    assert info.value.details["version"] == 2
    assert "t2" not in _tables(conn)
    assert "t1" in _tables(conn)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]
    assert not conn.in_transaction


def test_check_schema_empty_file(tmp_path: Path) -> None:
    c = sqlite3.connect(str(tmp_path / "e.db"), isolation_level=None)
    src = _src(tmp_path, {"0001_a.sql": "CREATE TABLE t1 (x);"})
    with pytest.raises(NotInitializedError):
        check_schema(c, source=src)
    assert _tables(c) == set()
    assert current_schema_version(c) == 0
    c.close()


def test_check_schema_behind(conn: sqlite3.Connection, tmp_path: Path) -> None:
    first = _src(tmp_path, {"0001_a.sql": "CREATE TABLE t1 (x);"})
    apply_migrations(conn, FixedClock(), source=first)
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE t2 (x);")
    with pytest.raises(NotInitializedError) as info:
        check_schema(conn, source=DirectoryMigrations(tmp_path))
    assert info.value.details == {"db_version": 1, "supported_version": 2}
    assert "t2" not in _tables(conn)
