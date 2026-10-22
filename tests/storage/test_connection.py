from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from garmin_archive.models import DatabaseNotFoundError, StorageError
from garmin_archive.storage.connection import (
    _read_only_uri,
    create_private_file,
    open_read_connection,
    open_write_connection,
    secure_sidecar_files,
    transaction,
)


def _make_db(path: Path) -> None:
    conn = open_write_connection(path, busy_timeout_ms=1000)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.close()


def test_write_connection_pragmas(tmp_path: Path) -> None:
    conn = open_write_connection(tmp_path / "a.db", busy_timeout_ms=1234)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert conn.isolation_level is None
    finally:
        conn.close()


def test_read_connection_rejects_writes(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _make_db(db)
    conn = open_read_connection(db, busy_timeout_ms=1000)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (v) VALUES ('x')")
    finally:
        conn.close()


def test_read_only_uri_shape_and_encoding(tmp_path: Path) -> None:
    uri = _read_only_uri(tmp_path / "my dir" / "a#b?.db")
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")
    assert "immutable" not in uri
    assert " " not in uri
    assert "%20" in uri and "%23" in uri and "%3F" in uri


def test_read_connection_special_chars_path(tmp_path: Path) -> None:
    db = tmp_path / "sp ace #1" / "a.db"
    db.parent.mkdir()
    _make_db(db)
    conn = open_read_connection(db, busy_timeout_ms=1000)
    conn.close()


def test_read_connection_missing_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    with pytest.raises(DatabaseNotFoundError):
        open_read_connection(db, busy_timeout_ms=1000)
    assert list(tmp_path.iterdir()) == []


def test_random_bytes_file_raises_storage_error(tmp_path: Path) -> None:
    db = tmp_path / "junk.db"
    db.write_bytes(os.urandom(4096))
    with pytest.raises(StorageError) as w:
        open_write_connection(db, busy_timeout_ms=1000)
    assert isinstance(w.value.__cause__, sqlite3.Error)
    # SQLite opens read-only connections lazily, so the failure may only surface
    # on the first query depending on the platform's SQLite build.
    with pytest.raises((StorageError, sqlite3.DatabaseError)):
        conn = open_read_connection(db, busy_timeout_ms=1000)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchall()


def test_transaction_commits(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _make_db(db)
    conn = open_write_connection(db, busy_timeout_ms=1000)
    with transaction(conn):
        conn.execute("INSERT INTO t (v) VALUES ('x')")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    conn.close()


def test_transaction_rolls_back_and_reraises(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _make_db(db)
    conn = open_write_connection(db, busy_timeout_ms=1000)
    with pytest.raises(RuntimeError, match="boom"), transaction(conn):
        conn.execute("INSERT INTO t (v) VALUES ('x')")
        raise RuntimeError("boom")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    conn.close()


def test_create_private_file_exclusive(tmp_path: Path) -> None:
    f = tmp_path / "new.db"
    create_private_file(f)
    assert f.exists() and f.stat().st_size == 0
    if sys.platform != "win32":
        assert f.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        create_private_file(f)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_secure_sidecar_files(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    conn = open_write_connection(db, busy_timeout_ms=1000)
    conn.execute("CREATE TABLE t (id INTEGER)")
    try:
        for suffix in ("-wal", "-shm"):
            side = tmp_path / ("a.db" + suffix)
            if side.exists():
                side.chmod(0o644)
        secure_sidecar_files(db)
        for suffix in ("-wal", "-shm"):
            side = tmp_path / ("a.db" + suffix)
            if side.exists():
                assert side.stat().st_mode & 0o777 == 0o600
    finally:
        conn.close()
