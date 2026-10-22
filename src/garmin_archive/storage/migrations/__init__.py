"""Migration model, sources and runner (SPEC §4)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

from garmin_archive.models import (
    Clock,
    MigrationChecksumError,
    NotInitializedError,
    SchemaVersionError,
    StorageError,
    utc_rfc3339,
)

_FILENAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")
_FORBIDDEN_RE = re.compile(r"\b(BEGIN|COMMIT|ROLLBACK)\b", re.IGNORECASE)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


class MigrationSource(Protocol):
    def load(self) -> Sequence[Migration]:
        """Return migrations sorted by version with unique versions."""
        ...


def _build_migration(filename: str, data: bytes) -> Migration:
    match = _FILENAME_RE.match(filename)
    if match is None:
        raise ValueError(f"Migration file name must look like NNNN_<slug>.sql: {filename}")
    sql = data.decode("utf-8")
    if _FORBIDDEN_RE.search(_LINE_COMMENT_RE.sub("", sql)):
        raise ValueError(f"Migration {filename} must not contain BEGIN, COMMIT or ROLLBACK")
    return Migration(
        version=int(match.group(1)),
        name=match.group(2),
        sql=sql,
        checksum=hashlib.sha256(data).hexdigest(),
    )


def _finalize(migrations: list[Migration]) -> list[Migration]:
    migrations.sort(key=lambda m: m.version)
    for previous, current in zip(migrations, migrations[1:], strict=False):
        if previous.version == current.version:
            raise ValueError(f"Duplicate migration version: {current.version}")
    return migrations


class PackagedMigrations:
    """Migrations shipped in ``garmin_archive.storage.migrations``."""

    def load(self) -> Sequence[Migration]:
        package = resources.files("garmin_archive.storage.migrations")
        found = [
            _build_migration(entry.name, entry.read_bytes())
            for entry in package.iterdir()
            if entry.is_file() and entry.name.endswith(".sql")
        ]
        return _finalize(found)


class DirectoryMigrations:
    """Migrations loaded from a filesystem directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Sequence[Migration]:
        found = [
            _build_migration(entry.name, entry.read_bytes())
            for entry in self._path.iterdir()
            if entry.is_file() and entry.name.endswith(".sql")
        ]
        return _finalize(found)


_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, "
    "applied_at_utc TEXT NOT NULL, "
    "checksum TEXT NOT NULL)"
)


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    return row is not None


def _applied(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(version): str(checksum) for version, checksum in rows}


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the maximum applied migration version, or 0 if none."""
    if not _table_exists(conn):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def verify(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> None:
    """Raise if the database is newer than the migrations or a checksum differs."""
    applied = _applied(conn)
    supported = max((m.version for m in migrations), default=0)
    if applied:
        db_version = max(applied)
        if db_version > supported:
            raise SchemaVersionError(
                f"Database schema version {db_version} is newer than supported {supported}",
                details={"db_version": db_version, "supported_version": supported},
            )
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationChecksumError(
                f"Checksum mismatch for applied migration {migration.version}",
                details={"version": migration.version},
            )


def apply_migrations(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    source: MigrationSource | None = None,
) -> list[int]:
    """Apply pending migrations atomically, returning the versions applied."""
    migrations = (source or PackagedMigrations()).load()
    conn.execute(_CREATE_TABLE)
    applied = _applied(conn)
    verify(conn, migrations)
    done: list[int] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        stamp = utc_rfc3339(clock.now())
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n;\n"
            "INSERT INTO schema_migrations(version, applied_at_utc, checksum) "
            f"VALUES ({int(migration.version)}, '{stamp}', '{migration.checksum}');\n"
            "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise StorageError(
                f"Migration {migration.version} failed: {exc}",
                details={"version": migration.version},
            ) from exc
        done.append(migration.version)
    return done


def check_schema(conn: sqlite3.Connection, *, source: MigrationSource | None = None) -> int:
    """Validate the schema without creating or changing anything."""
    migrations = (source or PackagedMigrations()).load()
    if not _table_exists(conn):
        raise NotInitializedError("Database is not initialized")
    verify(conn, migrations)
    supported = max((m.version for m in migrations), default=0)
    db_version = current_schema_version(conn)
    if db_version < supported:
        raise NotInitializedError(
            f"Database schema version {db_version} is behind supported {supported}",
            details={"db_version": db_version, "supported_version": supported},
        )
    return db_version
