"""CLI wiring for ``schema``, ``describe``, ``sample`` and ``sql`` (offline, JSON, exit-coded)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.cli import main


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


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    with ArchiveClient(ArchiveConfig(db_path=path)) as client:
        client.initialize()
    return path


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., Run]:
    def run(db_path: Path, *args: str) -> Run:
        client = httpx.Client(transport=httpx.MockTransport(_no_network))
        code = main(["--db", str(db_path), *args], http_client=client)
        return Run(code, capsys.readouterr().out)

    return run


def test_schema_prints_one_document(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "schema")
    assert result.code == 0
    assert result.out.count("\n") == 1
    assert result.doc["status"] == "ok"


def test_describe_and_sample(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "describe", "activities")
    assert result.code == 0
    assert result.out.count("\n") == 1
    sampled = cli(db, "sample", "activities", "--limit", "2")
    assert sampled.code == 0
    assert sampled.out.count("\n") == 1
    assert sampled.doc["status"] == "ok"


def test_sql_with_params(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "sql", "--query", "SELECT :x AS x", "--params", '{"x": 3}')
    assert result.code == 0
    assert result.out.count("\n") == 1
    assert "3" in result.out


def test_invalid_params_json_exits_2(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "sql", "--query", "SELECT 1", "--params", "{nope")
    assert result.code == 2
    assert result.doc["error"]["code"] == "INVALID_QUERY"


def test_write_statement_exits_2(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "sql", "--query", "DELETE FROM activities")
    assert result.code == 2
    assert result.doc["error"]["code"] == "INVALID_QUERY"


def test_timeout_exits_8(db: Path, cli: Callable[..., Run]) -> None:
    sql = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT COUNT(*) FROM c"
    result = cli(db, "sql", "--query", sql, "--timeout", "0.05")
    assert result.code == 8
    assert result.doc["error"]["code"] == "QUERY_TIMEOUT"


@pytest.mark.parametrize(
    "args",
    [
        ("schema",),
        ("describe", "activities"),
        ("sample", "activities"),
        ("sql", "--query", "SELECT 1"),
    ],
)
def test_missing_database_exits_5(
    tmp_path: Path, cli: Callable[..., Run], args: tuple[str, ...]
) -> None:
    result = cli(tmp_path / "missing.db", *args)
    assert result.code == 5
    assert result.doc["error"]["code"] == "DATABASE_NOT_FOUND"
