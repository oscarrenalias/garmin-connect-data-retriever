"""The ``ArchiveClient`` entry point for the archive library (SPEC §8)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from garmin_archive._version import __version__
from garmin_archive.config import ArchiveConfig, resolve_db_path
from garmin_archive.models import Clock, SystemClock
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
