"""Acceptance criterion 10 of spec-939594b5: ``reprocess`` is offline, JSON and exit-coded."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.cli import main
from garmin_archive.storage.connection import open_write_connection
from garmin_archive.storage.lock import writer_lock
from helpers.archive_seed import FixedClock, seed_activity, summary_copy
from helpers.fitbuilder import running_fit
from helpers.zipbuilder import build_zip

IDS = ("9000000001", "9000000002")


class RequestMade(AssertionError):
    pass


def _no_network(request: httpx.Request) -> httpx.Response:
    raise RequestMade(f"unexpected request: {request.url}")


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
    clock = FixedClock()
    with ArchiveClient(ArchiveConfig(db_path=path), clock=clock) as client:
        client.initialize()
    with closing(open_write_connection(path, busy_timeout_ms=1000)) as conn:
        for aid in IDS:
            seed_activity(
                conn,
                clock,
                aid,
                summary=summary_copy("running-1", aid),
                original_file=build_zip({"a.fit": running_fit(record_count=10)}),
            )
    return path


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., Run]:
    def run(db_path: Path | None, *args: str) -> Run:
        argv = ["--db", str(db_path)] if db_path is not None else []
        client = httpx.Client(transport=httpx.MockTransport(_no_network))
        code = main([*argv, "reprocess", *args], http_client=client)
        return Run(code, capsys.readouterr().out)

    return run


def _scalar(db: Path, sql: str) -> Any:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql).fetchone()[0]


def test_reprocess_all_prints_one_json_document(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db)
    assert result.code == 0
    assert result.out.count("\n") == 1
    data = result.doc["data"]
    assert data == {"processed": 2, "partial": 0, "failed": 0}
    assert _scalar(db, "SELECT COUNT(*) FROM activities") == 2
    assert _scalar(db, "SELECT COUNT(*) FROM activity_samples") == 20


def test_reprocess_runs_regardless_of_fingerprints(db: Path, cli: Callable[..., Run]) -> None:
    assert cli(db).code == 0
    assert cli(db).doc["data"]["processed"] == 2
    assert _scalar(db, "SELECT COUNT(*) FROM processing_runs") == 4
    assert _scalar(db, "SELECT COUNT(*) FROM activity_samples") == 20


def test_reprocess_single_activity(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "--activity-id", IDS[0])
    assert result.code == 0
    assert result.doc["data"] == {"processed": 1, "partial": 0, "failed": 0}
    assert _scalar(db, "SELECT COUNT(*) FROM activities") == 1


def test_unknown_activity_id_exits_7(db: Path, cli: Callable[..., Run]) -> None:
    result = cli(db, "--activity-id", "555")
    assert result.code == 7
    assert result.doc["data"] == {"processed": 0, "partial": 0, "failed": 1}


def test_failing_activity_exits_7(db: Path, cli: Callable[..., Run]) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            f"CREATE TRIGGER boom BEFORE INSERT ON activity_laps WHEN NEW.activity_id = '{IDS[0]}'"
            " BEGIN SELECT RAISE(ABORT, 'x'); END"
        )
        conn.commit()
    result = cli(db)
    assert result.code == 7
    assert result.doc["data"] == {"processed": 1, "partial": 0, "failed": 1}


def test_partial_only_exits_0(tmp_path: Path, cli: Callable[..., Run]) -> None:
    path = tmp_path / "p.db"
    clock = FixedClock()
    with ArchiveClient(ArchiveConfig(db_path=path), clock=clock) as client:
        client.initialize()
    with closing(open_write_connection(path, busy_timeout_ms=1000)) as conn:
        seed_activity(conn, clock, IDS[0], summary=summary_copy("running-1", IDS[0]))
    result = cli(path)
    assert result.code == 0
    assert result.doc["data"] == {"processed": 0, "partial": 1, "failed": 0}


def test_works_without_api_key(
    db: Path, cli: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    monkeypatch.delenv("GARMIN_CONNECT_API_KEY", raising=False)
    assert cli(db).code == 0


def test_works_with_no_http_client_injected(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(db), "reprocess"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["processed"] == 2


def test_missing_database_is_a_storage_error(tmp_path: Path, cli: Callable[..., Run]) -> None:
    result = cli(tmp_path / "missing.db")
    assert result.code == 5
    assert result.doc["error"]["code"] == "DATABASE_NOT_FOUND"


def test_locked_database_exits_5(db: Path, cli: Callable[..., Run]) -> None:
    with writer_lock(db.resolve()):
        result = cli(db)
    assert result.code == 5
    assert result.doc["error"]["code"] == "ARCHIVE_LOCKED"
