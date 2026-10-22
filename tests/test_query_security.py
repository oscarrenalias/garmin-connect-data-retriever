from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from garmin_archive.models import InvalidQueryError
from garmin_archive.query import authorizer, execute_guarded, harden_connection
from garmin_archive.storage.connection import open_read_connection
from garmin_archive.storage.migrations import apply_migrations, current_schema_version


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "t.db"
    w = sqlite3.connect(path, isolation_level=None)
    apply_migrations(w, FixedClock())
    w.close()
    c = open_read_connection(path, busy_timeout_ms=1000)
    harden_connection(c)
    yield c
    c.close()


def test_callback_decisions() -> None:
    ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == ok
    assert authorizer(sqlite3.SQLITE_RECURSIVE, None, None, None, None) == ok
    assert authorizer(sqlite3.SQLITE_READ, "activities", "id", "main", None) == ok
    assert authorizer(sqlite3.SQLITE_READ, "json_each", "value", None, None) == ok
    assert authorizer(sqlite3.SQLITE_READ, "", "", None, None) == ok
    assert authorizer(sqlite3.SQLITE_READ, "schema_migrations", "version", "main", None) == deny
    assert authorizer(sqlite3.SQLITE_FUNCTION, None, "abs", None, None) == ok
    assert authorizer(sqlite3.SQLITE_FUNCTION, None, "LOAD_EXTENSION", None, None) == deny
    assert authorizer(sqlite3.SQLITE_PRAGMA, "journal_mode", None, None, None) == deny
    assert authorizer(sqlite3.SQLITE_ATTACH, "x.db", None, None, None) == deny
    assert authorizer(sqlite3.SQLITE_INSERT, "activities", None, "main", None) == deny
    assert authorizer(sqlite3.SQLITE_TRANSACTION, "BEGIN", None, None, None) == deny


def test_schema_version_returned_before_authorizer(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    w = sqlite3.connect(path, isolation_level=None)
    apply_migrations(w, FixedClock())
    expected = current_schema_version(w)
    w.close()
    c = open_read_connection(path, busy_timeout_ms=1000)
    assert harden_connection(c) == expected
    with pytest.raises(InvalidQueryError):
        execute_guarded(c, "SELECT * FROM schema_migrations")
    c.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM activities",
        "SELECT count(*) FROM activities",
        "SELECT count(*) FROM v_running",
        "SELECT value FROM json_each('[1,2]')",
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<3) SELECT * FROM c",
        "SELECT abs(-1), lower('A')",
    ],
)
def test_allowed(conn: sqlite3.Connection, sql: str) -> None:
    execute_guarded(conn, sql).fetchall()


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA table_info(activities)",
        "SELECT * FROM pragma_table_info('activities')",
        "ATTACH DATABASE ':memory:' AS x",
        "INSERT INTO sync_runs DEFAULT VALUES",
        "DELETE FROM activities",
        "CREATE TABLE t(x)",
        "DROP TABLE activities",
        "BEGIN",
        "SELECT load_extension('x')",
        "SELECT * FROM schema_migrations",
        "SELECT * FROM sqlite_master",
        "SELECT count(*) FROM sqlite_master",
        "SELECT count(*) FROM sqlite_temp_master",
        "WITH v_running AS (SELECT * FROM schema_migrations) SELECT * FROM v_running",
        "SELEC nonsense",
    ],
)
def test_denied(conn: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(InvalidQueryError):
        execute_guarded(conn, sql)
