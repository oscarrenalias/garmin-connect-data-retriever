"""End-to-end acceptance tests for the query commands (spec query-commands, AC 2-8)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.cli import main

MIB = 1024 * 1024
SUMMARY_BODY = json.dumps({"summaryDTO": {"distance": 5000.5}, "tags": ["a", "b", "c"]})
BLOB = b"\x00\x01\x02\x03\x04\xff"


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request: {request.url}")


@dataclass
class Run:
    code: int
    out: str

    @property
    def doc(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.out)
        return loaded


def _insert_activity(conn: sqlite3.Connection, activity_id: str, type_key: str, n: int) -> None:
    conn.execute(
        "INSERT INTO activity_archive (activity_id, first_seen_at_utc, last_seen_at_utc) "
        "VALUES (?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (activity_id,),
    )
    conn.execute(
        "INSERT INTO activities (activity_id, processing_run_id, type_key, name, start_at_utc, "
        "distance_m, processing_status) VALUES (?, 1, ?, ?, ?, ?, 'processed')",
        (activity_id, type_key, f"Run {n}", f"2026-01-{n % 28 + 1:02d}T06:00:00Z", 1000.0 * n),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    with ArchiveClient(ArchiveConfig(db_path=path)) as client:
        client.initialize()
    conn = sqlite3.connect(path)
    try:
        for n in range(1, 6):
            _insert_activity(conn, str(100 + n), "running" if n % 2 else "cycling", n)
        conn.execute(
            "INSERT INTO raw_payloads (resource_key, activity_id, kind, content_type, body_text, "
            "body_blob, sha256, byte_count, first_seen_at_utc) VALUES "
            "('summary:101', '101', 'summary', 'application/json', ?, ?, 'aa', ?, "
            "'2026-01-01T00:00:00Z')",
            (SUMMARY_BODY, SUMMARY_BODY.encode(), len(SUMMARY_BODY)),
        )
        conn.execute(
            "INSERT INTO raw_payloads (resource_key, activity_id, kind, content_type, body_text, "
            "body_blob, sha256, byte_count, first_seen_at_utc) VALUES "
            "('file:101', '101', 'original_file', 'application/zip', NULL, ?, 'bb', ?, "
            "'2026-01-01T00:00:00Z')",
            (BLOB, len(BLOB)),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., Run]:
    def run(db_path: Path, *args: str) -> Run:
        client = httpx.Client(transport=httpx.MockTransport(_no_network))
        code = main(["--db", str(db_path), *args], http_client=client)
        return Run(code, capsys.readouterr().out)

    return run


def _sql(cli: Callable[..., Run], db: Path, query: str, *extra: str) -> Run:
    return cli(db, "sql", "--query", query, *extra)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- AC2: allowed queries -------------------------------------------------------------------


ALLOWED = {
    "cte_window": (
        "WITH r AS (SELECT activity_id, distance_m FROM activities) "
        "SELECT activity_id, sum(distance_m) OVER (ORDER BY activity_id) AS running_total FROM r"
    ),
    "join": (
        "SELECT a.activity_id, p.kind FROM activities a "
        "JOIN raw_payloads p ON p.activity_id = a.activity_id"
    ),
    "count_star": "SELECT count(*) FROM activities",
    "count_star_recursive_cte": (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) "
        "SELECT count(*) FROM r"
    ),
    "count_star_cte": "WITH r AS (SELECT activity_id FROM activities) SELECT count(*) FROM r",
    "json_extract": (
        "SELECT json_extract(body_text, '$.summaryDTO.distance') FROM raw_payloads "
        "WHERE body_text IS NOT NULL"
    ),
    "json_each": (
        "SELECT j.value FROM raw_payloads p, json_each(p.body_text, '$.tags') j "
        "WHERE p.body_text IS NOT NULL"
    ),
    "printf": "SELECT printf('%.1f km', distance_m / 1000.0) FROM activities",
    "strftime": "SELECT strftime('%Y-%m', start_at_utc) FROM activities",
    "view": "SELECT activity_id, include_in_volume_totals FROM v_activity_summary",
}


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_allowed_queries_succeed(db: Path, cli: Callable[..., Run], name: str) -> None:
    result = _sql(cli, db, ALLOWED[name])
    assert result.code == 0, result.out
    assert result.doc["status"] == "ok"
    assert result.doc["meta"]["row_count"] >= 1


def test_allowed_query_values(db: Path, cli: Callable[..., Run]) -> None:
    assert _sql(cli, db, ALLOWED["count_star"]).doc["rows"] == [[5]]
    assert _sql(cli, db, ALLOWED["json_extract"]).doc["rows"] == [[5000.5]]
    assert _sql(cli, db, ALLOWED["json_each"]).doc["rows"] == [["a"], ["b"], ["c"]]
    assert _sql(cli, db, "SELECT printf('%03d', 7)").doc["rows"] == [["007"]]
    assert _sql(cli, db, "SELECT strftime('%Y', '2026-03-04T05:06:07Z')").doc["rows"] == [["2026"]]


# --- AC3: rejected statements ---------------------------------------------------------------

REJECTED = [
    "INSERT INTO activities (activity_id, processing_run_id, processing_status) "
    "VALUES ('x', 1, 'p')",
    "UPDATE activities SET name = 'x'",
    "DELETE FROM activities",
    "CREATE TABLE t (a)",
    "DROP TABLE activities",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA table_info(activities)",
    "SELECT load_extension('x')",
    "SELECT 1; SELECT 2",
    "SELECT * FROM sqlite_master",
    "SELECT count(*) FROM sqlite_master",
    "SELECT count(*) FROM sqlite_temp_master",
    "SELECT * FROM schema_migrations",
    "BEGIN",
    "WITH v_activity_summary AS (SELECT * FROM schema_migrations) SELECT * FROM v_activity_summary",
]


@pytest.mark.parametrize("query", REJECTED)
def test_rejected_statements_exit_2_and_db_unchanged(
    db: Path, cli: Callable[..., Run], query: str
) -> None:
    before = _digest(db)
    result = _sql(cli, db, query)
    assert result.code == 2, result.out
    assert result.doc["status"] == "error"
    assert result.doc["error"]["code"] == "INVALID_QUERY"
    assert _digest(db) == before
    assert _sql(cli, db, "SELECT count(*) FROM activities").doc["rows"] == [[5]]


# --- AC4: params ----------------------------------------------------------------------------


def test_params_bind_by_name(db: Path, cli: Callable[..., Run]) -> None:
    result = _sql(
        cli,
        db,
        "SELECT count(*) FROM activities WHERE type_key = :t",
        "--params",
        '{"t": "running"}',
    )
    assert result.code == 0
    assert result.doc["rows"] == [[3]]


def test_params_injection_is_data(db: Path, cli: Callable[..., Run]) -> None:
    evil = "'; DROP TABLE activities; --"
    before = _digest(db)
    result = _sql(cli, db, "SELECT :t AS t", "--params", json.dumps({"t": evil}))
    assert result.code == 0
    assert result.doc["rows"] == [[evil]]
    matched = _sql(
        cli,
        db,
        "SELECT count(*) FROM activities WHERE type_key = :t",
        "--params",
        json.dumps({"t": evil}),
    )
    assert matched.doc["rows"] == [[0]]
    assert _digest(db) == before
    assert _sql(cli, db, "SELECT count(*) FROM activities").doc["rows"] == [[5]]


# --- AC5: limits ----------------------------------------------------------------------------


@pytest.fixture
def big_db(db: Path) -> Path:
    conn = sqlite3.connect(db)
    try:
        for n in range(1000, 7000):
            _insert_activity(conn, f"big-{n}", "running", n)
        conn.commit()
    finally:
        conn.close()
    return db


BIG_QUERY = "SELECT activity_id FROM activities WHERE activity_id LIKE 'big-%'"


def test_default_limit_truncates_at_5000(big_db: Path, cli: Callable[..., Run]) -> None:
    result = _sql(cli, big_db, BIG_QUERY)
    assert result.code == 0
    assert len(result.doc["rows"]) == 5000
    assert result.doc["meta"]["truncated"] is True
    assert result.doc["meta"]["limit_applied"] == 5000


def test_raised_limit_returns_all(big_db: Path, cli: Callable[..., Run]) -> None:
    result = _sql(cli, big_db, BIG_QUERY, "--limit", "100000")
    assert result.code == 0
    assert len(result.doc["rows"]) == 6000
    assert result.doc["meta"]["truncated"] is False
    assert result.doc["meta"]["limit_applied"] == 100000


def test_query_limit_clause_does_not_bypass_limit(big_db: Path, cli: Callable[..., Run]) -> None:
    result = _sql(cli, big_db, BIG_QUERY + " LIMIT 999999")
    assert result.code == 0
    assert len(result.doc["rows"]) == 5000
    assert result.doc["meta"]["truncated"] is True


def test_infinite_recursive_cte_times_out(db: Path, cli: Callable[..., Run]) -> None:
    started = time.monotonic()
    result = _sql(
        cli,
        db,
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
        "SELECT n FROM r WHERE n < 0",
        "--timeout",
        "1",
    )
    elapsed = time.monotonic() - started
    assert result.code == 8, result.out
    assert result.doc["error"]["code"] == "QUERY_TIMEOUT"
    assert elapsed < 15


def test_20_mib_text_truncated_with_valid_json(db: Path, cli: Callable[..., Run]) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO raw_payloads (resource_key, kind, body_text, body_blob, sha256, "
            "byte_count, first_seen_at_utc) VALUES ('big', 'summary', ?, x'00', 'cc', 1, "
            "'2026-01-01T00:00:00Z')",
            ("x" * (20 * MIB),),
        )
        conn.commit()
    finally:
        conn.close()
    result = _sql(cli, db, "SELECT body_text FROM raw_payloads WHERE resource_key = 'big'")
    assert result.code == 0
    doc = result.doc  # parses: output is valid JSON
    assert doc["meta"]["truncated"] is True
    assert len(result.out) < 12 * MIB


# --- AC6: BLOB metadata ---------------------------------------------------------------------


def test_blob_column_is_metadata(db: Path, cli: Callable[..., Run]) -> None:
    result = _sql(cli, db, "SELECT body_blob FROM raw_payloads WHERE kind = 'original_file'")
    assert result.code == 0
    assert result.doc["rows"] == [[{"type": "blob", "bytes": len(BLOB)}]]
    assert BLOB.hex() not in result.out


# --- AC7: schema / describe / sample --------------------------------------------------------


def test_schema_shape(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "schema")
    assert result.code == 0
    assert result.out.count("\n") == 1
    doc = result.doc
    assert doc["status"] == "ok"
    objects = {o["name"]: o for o in doc["data"]["objects"]}
    assert {"activities", "raw_payloads", "v_activity_summary"} <= set(objects)
    assert "schema_migrations" not in objects
    assert "sqlite_master" not in objects
    for obj in objects.values():
        assert obj["kind"] in ("table", "view")
        assert obj["description"]
        assert obj["grain"]
        assert all({"name", "type", "pk"} <= set(c) for c in obj["columns"])
    assert objects["activities"]["kind"] == "table"
    assert objects["v_activity_summary"]["kind"] == "view"
    assert "schema_version" in json.dumps(doc)


def test_describe_activities_shape(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "describe", "activities")
    assert result.code == 0
    assert result.out.count("\n") == 1
    doc = result.doc
    assert doc["status"] == "ok"
    data = doc["data"]
    assert data["name"] == "activities"
    assert data["kind"] == "table"
    assert data["row_count"] == 5
    pk = [c["name"] for c in data["columns"] if c["pk"]]
    assert pk == ["activity_id"]


def test_sample_view_limit(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "sample", "v_activity_summary", "--limit", "3")
    assert result.code == 0
    assert result.out.count("\n") == 1
    doc = result.doc
    assert doc["status"] == "ok"
    assert len(doc["rows"]) == 3
    assert doc["columns"][0] == "activity_id"
    assert [r[0] for r in doc["rows"]] == ["101", "102", "103"]


def test_sample_shows_blob_metadata(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "sample", "raw_payloads")
    assert result.code == 0
    cols = result.doc["columns"]
    cell = result.doc["rows"][1][cols.index("body_blob")]
    assert cell == {"type": "blob", "bytes": len(BLOB)}


@pytest.mark.parametrize("args", [("describe", "sqlite_master"), ("sample", "sqlite_master")])
def test_sqlite_master_rejected(db: Path, cli: Callable[..., Run], args: tuple[str, ...]) -> None:
    result = cli(db, *args)
    assert result.code == 2
    assert result.doc["error"]["code"] == "INVALID_QUERY"


@pytest.mark.parametrize("name", ["nope", "activities; DROP TABLE activities", 'activities"'])
def test_describe_unknown_or_injected_name_rejected(
    db: Path, cli: Callable[..., Run], name: str
) -> None:
    before = _digest(db)
    result = cli(db, "describe", name)
    assert result.code == 2
    assert _digest(db) == before


# --- AC8: missing DB, no network ------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("schema",),
        ("describe", "activities"),
        ("sample", "activities"),
        ("sql", "--query", "SELECT 1"),
    ],
)
def test_missing_db_exits_5_and_creates_nothing(
    tmp_path: Path, cli: Callable[..., Run], args: tuple[str, ...]
) -> None:
    missing = tmp_path / "sub" / "missing.db"
    result = cli(missing, *args)
    assert result.code == 5
    assert result.doc["error"]["code"] == "DATABASE_NOT_FOUND"
    assert not missing.exists()
    assert not missing.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_query_commands_never_use_network(db: Path, cli: Callable[..., Run]) -> None:
    # The injected transport raises AssertionError on any request; every command must still succeed.
    for args in (
        ("schema",),
        ("describe", "activities"),
        ("sample", "activities"),
        ("sql", "--query", "SELECT 1"),
    ):
        assert cli(db, *args).code == 0
