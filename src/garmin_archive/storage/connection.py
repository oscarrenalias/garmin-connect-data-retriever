"""SQLite connection helpers and the transaction context manager."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from garmin_archive.models import DatabaseNotFoundError, StorageError

_PRIVATE_MODE = 0o600


def _read_only_uri(path: Path) -> str:
    """Build the read-only SQLite URI for ``path`` (never ``immutable=1``)."""
    absolute = Path(path).absolute().as_posix()
    if not absolute.startswith("/"):
        # Windows drive paths such as C:/x need a leading slash in file: URIs.
        absolute = "/" + absolute
    return f"file:{quote(absolute, safe='/')}?mode=ro"


def open_write_connection(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open a read-write WAL connection with explicit transaction control."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = str(row[0]).lower() if row else ""
        if mode != "wal":
            raise StorageError(f"Could not enable WAL journal mode (got {mode!r})")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous=NORMAL")
    except (sqlite3.Error, OSError) as exc:
        if conn is not None:
            conn.close()
        raise StorageError(f"Could not open database: {exc}") from exc
    except StorageError:
        if conn is not None:
            conn.close()
        raise
    return conn


def open_read_connection(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open a query-only connection; never creates the database file."""
    if not Path(path).exists():
        raise DatabaseNotFoundError(f"Database not found: {path}")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_read_only_uri(path), uri=True, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    except (sqlite3.Error, OSError) as exc:
        if conn is not None:
            conn.close()
        raise StorageError(f"Could not open database: {exc}") from exc
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a ``BEGIN IMMEDIATE`` transaction: commit on success, roll back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def create_private_file(path: Path) -> None:
    """Exclusively create an empty file with mode 0o600 (mode skipped on Windows)."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if sys.platform == "win32":
        fd = os.open(path, flags)
    else:
        fd = os.open(path, flags, _PRIVATE_MODE)
    os.close(fd)


def secure_sidecar_files(path: Path) -> None:
    """chmod 0o600 any existing ``-wal``/``-shm`` files next to ``path``."""
    if sys.platform == "win32":
        return
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.chmod(_PRIVATE_MODE)
