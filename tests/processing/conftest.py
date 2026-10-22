from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.storage.connection import open_write_connection
from helpers.archive_seed import FixedClock


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def db_path(tmp_path: Path, clock: FixedClock) -> Path:
    path = tmp_path / "archive.db"
    with ArchiveClient(ArchiveConfig(db_path=path), clock=clock) as client:
        client.initialize()
    return path


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_write_connection(db_path, busy_timeout_ms=1000)
    try:
        yield connection
    finally:
        connection.close()
