from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from garmin_archive.models import InvalidQueryError
from garmin_archive.query import describe, sample, schema
from garmin_archive.storage.connection import open_read_connection


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "t.db"
    w = sqlite3.connect(path, isolation_level=None)
    w.execute("CREATE TABLE schema_migrations (version INTEGER)")
    w.execute("INSERT INTO schema_migrations VALUES (3)")
    w.execute("CREATE TABLE activities (id TEXT PRIMARY KEY, name TEXT)")
    w.executemany("INSERT INTO activities VALUES (?, ?)", [(i, f"a{i}") for i in ("c", "a", "b")])
    w.execute("CREATE TABLE sync_runs (id INTEGER, note TEXT)")
    w.executemany("INSERT INTO sync_runs VALUES (?, ?)", [(2, "x"), (1, "y")])
    w.execute("CREATE TABLE raw_payloads (id INTEGER PRIMARY KEY, body BLOB)")
    w.execute("INSERT INTO raw_payloads VALUES (1, x'0102')")
    w.execute("CREATE VIEW v_running AS SELECT name, id FROM activities")
    w.close()
    c = open_read_connection(path, busy_timeout_ms=1000)
    yield c
    c.close()


def test_schema_lists_existing_registry_objects(conn: sqlite3.Connection) -> None:
    doc = schema(conn)
    assert doc["status"] == "ok"
    objs = {o["name"]: o for o in doc["data"]["objects"]}
    assert set(objs) == {"activities", "sync_runs", "raw_payloads", "v_running"}
    assert "schema_migrations" not in objs
    assert objs["activities"]["kind"] == "table"
    assert objs["v_running"]["kind"] == "view"
    assert objs["activities"]["grain"]
    assert objs["activities"]["description"]
    assert objs["activities"]["columns"][0] == {"name": "id", "type": "TEXT", "pk": True}
    assert isinstance(doc["data"]["schema_version"], int)


def test_describe_has_row_count(conn: sqlite3.Connection) -> None:
    data = describe(conn, "activities")["data"]
    assert data["row_count"] == 3
    assert data["name"] == "activities"
    assert [c["name"] for c in data["columns"]] == ["id", "name"]


@pytest.mark.parametrize("name", ["sqlite_master", "nope", 'activities"; --', "activity_laps"])
def test_unknown_names_rejected(conn: sqlite3.Connection, name: str) -> None:
    with pytest.raises(InvalidQueryError):
        describe(conn, name)
    with pytest.raises(InvalidQueryError):
        sample(conn, name)


def test_sample_orders_by_primary_key(conn: sqlite3.Connection) -> None:
    doc = sample(conn, "activities", 2)
    assert [r[0] for r in doc["rows"]] == ["a", "b"]
    assert doc["meta"]["truncated"] is True
    assert doc["meta"]["limit_applied"] == 2


def test_sample_orders_by_rowid_for_tables(conn: sqlite3.Connection) -> None:
    doc = sample(conn, "sync_runs")
    assert [r[0] for r in doc["rows"]] == [2, 1]
    assert doc["meta"]["truncated"] is False


def test_sample_orders_view_by_first_column(conn: sqlite3.Connection) -> None:
    doc = sample(conn, "v_running")
    assert doc["columns"] == ["name", "id"]
    assert [r[0] for r in doc["rows"]] == sorted(r[0] for r in doc["rows"])


def test_sample_blob_as_metadata(conn: sqlite3.Connection) -> None:
    doc = sample(conn, "raw_payloads")
    assert doc["rows"] == [[1, {"type": "blob", "bytes": 2}]]


def test_sample_output_budget(conn: sqlite3.Connection) -> None:
    doc = sample(conn, "activities", 3, output_budget=30)
    assert doc["meta"]["truncated"] is True
    assert len(doc["rows"]) < 3


@pytest.mark.parametrize("limit", [0, -1, 100_001, True])
def test_sample_bad_limit(conn: sqlite3.Connection, limit: int) -> None:
    with pytest.raises(InvalidQueryError):
        sample(conn, "activities", limit)
