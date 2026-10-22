"""The ``ArchiveClient`` entry point for the archive library (SPEC §8)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from garmin_archive._version import __version__
from garmin_archive.config import ArchiveConfig, resolve_db_path
from garmin_archive.models import Clock, DatabaseNotFoundError, SystemClock
from garmin_archive.processing.batch import BatchCounters, reprocess
from garmin_archive.processing.form_import import FormImportResult, import_form
from garmin_archive.query import (
    DEFAULT_LIMIT,
    DEFAULT_TIMEOUT_S,
    describe,
    harden_connection,
    run_query,
    sample,
    schema,
    tabular_result,
)
from garmin_archive.storage.connection import (
    create_private_file,
    open_read_connection,
    open_write_connection,
    secure_sidecar_files,
    transaction,
)
from garmin_archive.storage.lock import writer_lock
from garmin_archive.storage.metadata import set_metadata
from garmin_archive.storage.migrations import (
    MigrationSource,
    apply_migrations,
    check_schema,
    current_schema_version,
)
from garmin_archive.sync.update import UpdateApi, UpdateResult, run_update

SCHEMA_METADATA_KEY = "schema"


@dataclass(frozen=True)
class InitResult:
    """Outcome of ``ArchiveClient.initialize``."""

    db_path: Path
    schema_version: int
    created: bool
    applied_migrations: list[int]


class ArchiveClient:
    """Library entry point. Holds no database connection between calls."""

    def __init__(
        self,
        config: ArchiveConfig | None = None,
        *,
        clock: Clock | None = None,
        environ: Mapping[str, str] | None = None,
        migrations: MigrationSource | None = None,
    ) -> None:
        self._config = config or ArchiveConfig()
        self._clock: Clock = clock or SystemClock()
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._migrations = migrations
        self._closed = False

    @property
    def db_path(self) -> Path:
        """The resolved database path."""
        return resolve_db_path(self._config, self._environ)

    def __enter__(self) -> ArchiveClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release client resources; safe to call more than once."""
        self._closed = True

    def initialize(self) -> InitResult:
        """Create the database if needed and apply pending migrations."""
        path = self.db_path
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with writer_lock(path):
            if not path.exists():
                created = True
                create_private_file(path)
            conn = open_write_connection(path, busy_timeout_ms=self._config.busy_timeout_ms)
            try:
                applied = apply_migrations(conn, self._clock, source=self._migrations)
                version = current_schema_version(conn)
                with transaction(conn):
                    set_metadata(
                        conn,
                        SCHEMA_METADATA_KEY,
                        {"schema_version": version, "package_version": __version__},
                    )
                secure_sidecar_files(path)
            finally:
                conn.close()
        return InitResult(
            db_path=path,
            schema_version=version,
            created=created,
            applied_migrations=applied,
        )

    def _open_existing_read(self) -> sqlite3.Connection:
        """Open a validated read-only connection; never creates or migrates."""
        conn = open_read_connection(self.db_path, busy_timeout_ms=self._config.busy_timeout_ms)
        try:
            check_schema(conn, source=self._migrations)
        except BaseException:
            conn.close()
            raise
        return conn

    def update(
        self,
        api: UpdateApi,
        *,
        full: bool = False,
        stop_flag: Callable[[], bool] = lambda: False,
    ) -> UpdateResult:
        """Discover and archive every activity; never creates or migrates the database."""
        path = self.db_path
        if not path.exists():
            raise DatabaseNotFoundError(f"Database not found: {path}")
        with writer_lock(path):
            conn = open_write_connection(path, busy_timeout_ms=self._config.busy_timeout_ms)
            try:
                check_schema(conn, source=self._migrations)
                return run_update(api, conn, self._clock, full=full, stop_flag=stop_flag)
            finally:
                conn.close()
                secure_sidecar_files(path)

    def reprocess(self, activity_id: str | None = None) -> BatchCounters:
        """Rebuild derived tables from archived payloads; local only, never touches the network.

        ``reprocess`` takes the writer lock itself, so none is held here.
        """
        path = self.db_path
        if not path.exists():
            raise DatabaseNotFoundError(f"Database not found: {path}")
        conn = open_write_connection(path, busy_timeout_ms=self._config.busy_timeout_ms)
        try:
            check_schema(conn, source=self._migrations)
            return reprocess(conn, self._clock, activity_id)
        finally:
            conn.close()
            secure_sidecar_files(path)

    def import_form(
        self,
        *,
        paths: Sequence[Path] | None = None,
        url: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> FormImportResult:
        """Import FORM goggles FIT files from local ``paths`` or a download ``url``."""
        path = self.db_path
        if not path.exists():
            raise DatabaseNotFoundError(f"Database not found: {path}")
        with writer_lock(path):
            conn = open_write_connection(path, busy_timeout_ms=self._config.busy_timeout_ms)
            try:
                check_schema(conn, source=self._migrations)
                return import_form(conn, self._clock, paths=paths, url=url, http_client=http_client)
            finally:
                conn.close()
                secure_sidecar_files(path)

    def schema(self) -> dict[str, Any]:
        """List the queryable objects in the archive; local only."""
        conn = self._open_existing_read()
        try:
            return schema(conn)
        finally:
            conn.close()

    def describe(self, name: str) -> dict[str, Any]:
        """Describe one queryable object, including its row count; local only."""
        conn = self._open_existing_read()
        try:
            return describe(conn, name)
        finally:
            conn.close()

    def sample(self, name: str, limit: int = 5) -> dict[str, Any]:
        """Return the first rows of a queryable object; local only."""
        conn = self._open_existing_read()
        try:
            return sample(conn, name, limit)
        finally:
            conn.close()

    def query(
        self,
        sql: str,
        parameters: Mapping[str, object] | None = None,
        limit: int = DEFAULT_LIMIT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Run one read-only SQL statement with bound parameters; local only."""
        conn = self._open_existing_read()
        try:
            version = harden_connection(conn)
            result = run_query(conn, sql, parameters, limit=limit, timeout_s=timeout_s)
            return tabular_result(result, limit_applied=limit, schema_version=version)
        finally:
            conn.close()
