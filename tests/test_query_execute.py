from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from garmin_archive.models import InvalidQueryError, QueryTimeoutError
from garmin_archive.query import QueryResult, harden_connection, run_query, tabular_result
from garmin_archive.storage.connection import open_read_connection


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "t.db"
    w = sqlite3.connect(path, isolation_level=None)
    w.execute("CREATE TABLE activities (id TEXT, name TEXT)")
    w.executemany("INSERT INTO activities VALUES (?, ?)", [(str(i), f"a{i}") for i in range(10)])
    w.close()
    c = open_read_connection(path, busy_timeout_ms=1000)
    harden_connection(c)
    yield c
    c.close()


def test_named_params_and_injection_as_data(conn: sqlite3.Connection) -> None:
    r = run_query(conn, "SELECT id FROM activities WHERE name = :n", {"n": "a3"})
    assert r.columns == ["id"]
    assert r.rows == [("3",)]
    assert not r.truncated
    r = run_query(conn, "SELECT id FROM activities WHERE name = :n", {"n": "a3' OR '1'='1"})
    assert r.rows == []


@pytest.mark.parametrize("params", [[1], "x", 5, {1: "a"}])
def test_non_object_params_rejected(conn: sqlite3.Connection, params: object) -> None:
    with pytest.raises(InvalidQueryError):
        run_query(conn, "SELECT 1", params)


def test_limit_enforced_over_user_limit(conn: sqlite3.Connection) -> None:
    r = run_query(conn, "SELECT id FROM activities LIMIT 999999", limit=3)
    assert len(r.rows) == 3
    assert r.truncated
    r = run_query(conn, "SELECT id FROM activities", limit=10)
    assert len(r.rows) == 10
    assert not r.truncated


@pytest.mark.parametrize("limit", [0, -1, 100_001, True])
def test_bad_limit(conn: sqlite3.Connection, limit: int) -> None:
    with pytest.raises(InvalidQueryError):
        run_query(conn, "SELECT 1", limit=limit)


@pytest.mark.parametrize("timeout", [0, -1])
def test_bad_timeout(conn: sqlite3.Connection, timeout: float) -> None:
    with pytest.raises(InvalidQueryError):
        run_query(conn, "SELECT 1", timeout_s=timeout)


def test_multiple_statements_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(InvalidQueryError):
        run_query(conn, "SELECT 1; SELECT 2")


def test_syntax_error_is_invalid_query(conn: sqlite3.Connection) -> None:
    with pytest.raises(InvalidQueryError):
        run_query(conn, "SELEC 1")


def test_infinite_cte_times_out(conn: sqlite3.Connection) -> None:
    ticks = iter(range(10_000))
    with pytest.raises(QueryTimeoutError):
        run_query(
            conn,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c",
            timeout_s=5,
            monotonic=lambda: float(next(ticks)),
        )


def test_infinite_cte_real_clock(conn: sqlite3.Connection) -> None:
    with pytest.raises(QueryTimeoutError):
        run_query(
            conn,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c",
            timeout_s=0.2,
        )


def test_interrupt_without_flag_is_invalid_query(conn: sqlite3.Connection) -> None:
    def boom(*_: object) -> None:
        raise sqlite3.OperationalError("interrupted")

    class Proxy:
        def set_progress_handler(self, *_: object) -> None:
            pass

        def execute(self, *a: object) -> None:
            boom()

    with pytest.raises(InvalidQueryError):
        run_query(Proxy(), "SELECT 1")  # type: ignore[arg-type]


def test_progress_handler_cleared(conn: sqlite3.Connection) -> None:
    run_query(conn, "SELECT 1")
    assert run_query(conn, "SELECT count(*) FROM activities").rows == [(10,)]


def _qr(rows: list[tuple[object, ...]], truncated: bool = False) -> QueryResult:
    return QueryResult(["a", "b"], rows, truncated, 7)


def test_tabular_shape_and_blob() -> None:
    doc = tabular_result(
        _qr([("x", b"\x00\x01\x02"), (None, 1.5)]), limit_applied=5000, schema_version=3
    )
    assert doc["status"] == "ok"
    assert doc["columns"] == ["a", "b"]
    assert doc["rows"] == [["x", {"type": "blob", "bytes": 3}], [None, 1.5]]
    assert doc["meta"] == {
        "row_count": 2,
        "truncated": False,
        "limit_applied": 5000,
        "execution_ms": 7,
        "schema_version": 3,
        "output_version": 1,
    }


def test_large_value_truncates_with_valid_json() -> None:
    big = "x" * (20 * 1024 * 1024)
    doc = tabular_result(
        _qr([("small", 1), (big, 2), ("later", 3)]), limit_applied=10, schema_version=1
    )
    assert doc["rows"] == [["small", 1]]
    assert doc["meta"]["truncated"] is True
    assert doc["meta"]["row_count"] == 1
    assert json.loads(json.dumps(doc)) == doc


def test_first_row_over_budget_gives_empty_rows() -> None:
    doc = tabular_result(
        _qr([("x" * 100, 1)]), limit_applied=10, schema_version=1, output_budget=50
    )
    assert doc["rows"] == []
    assert doc["meta"]["truncated"] is True
    assert doc["meta"]["row_count"] == 0


def test_truncated_flag_preserved_and_nonfinite_float_is_null() -> None:
    doc = tabular_result(
        _qr([("x", float("inf"))], truncated=True), limit_applied=1, schema_version=1
    )
    assert doc["rows"] == [["x", None]]
    assert doc["meta"]["truncated"] is True
    json.dumps(doc, allow_nan=False)
