"""CLI ``import-form``: exit codes, JSON output and URL secrecy."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from garmin_fit_sdk import Encoder

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.cli import main
from helpers.archive_seed import FixedClock
from helpers.zipbuilder import build_zip

URL = "https://formdata-eu-west-1.s3.eu-west-1.amazonaws.com/x.zip?X-Amz-Signature=sekrit42"


def _fit(manufacturer: str = "form", product: int = 0) -> bytes:
    encoder = Encoder()
    encoder.write_mesg(
        {
            "mesg_num": 0,
            "type": "activity",
            "manufacturer": manufacturer,
            "product": product,
            "serial_number": 987654321,
        }
    )
    return encoder.close()


def _unexpected(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request: {request.url}")


@dataclass
class Run:
    code: int
    out: str
    err: str

    @property
    def doc(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.out)
        return loaded


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    with ArchiveClient(ArchiveConfig(db_path=path), clock=FixedClock()) as client:
        client.initialize()
    return path


@pytest.fixture
def cli(db: Path, capsys: pytest.CaptureFixture[str]) -> Callable[..., Run]:
    def run(*args: str, handler: Callable[[httpx.Request], httpx.Response] = _unexpected) -> Run:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        code = main(["--db", str(db), "import-form", *args], http_client=client)
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return run


def _write(tmp_path: Path, name: str, body: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def test_files_ok_exits_0(cli: Callable[..., Run], tmp_path: Path) -> None:
    result = cli(_write(tmp_path, "a.fit", _fit()))
    assert result.code == 0
    assert result.doc["status"] == "ok"
    assert result.doc["data"]["imported"] == 1
    assert result.doc["data"]["rejected"] == []


def test_some_rejected_exits_6(cli: Callable[..., Run], tmp_path: Path) -> None:
    good = _write(tmp_path, "a.fit", _fit())
    bad = _write(tmp_path, "b.fit", _fit("garmin"))
    result = cli(good, bad)
    assert result.code == 6
    assert result.doc["data"]["imported"] == 1
    assert [r["file"] for r in result.doc["data"]["rejected"]] == ["b.fit"]


def test_nothing_valid_exits_2(cli: Callable[..., Run], tmp_path: Path) -> None:
    result = cli(_write(tmp_path, "b.fit", _fit("garmin")))
    assert result.code == 2
    assert result.doc["data"]["imported"] == 0


def test_no_arguments_exits_2(cli: Callable[..., Run]) -> None:
    result = cli()
    assert result.code == 2
    assert result.doc["status"] == "error"


def test_files_and_url_exits_2(cli: Callable[..., Run], tmp_path: Path) -> None:
    result = cli(_write(tmp_path, "a.fit", _fit()), "--url", URL)
    assert result.code == 2
    assert "sekrit42" not in result.out + result.err


def test_url_success_hides_query_string(cli: Callable[..., Run]) -> None:
    body = build_zip({"x.fit": _fit()})
    result = cli("--url", URL, handler=lambda r: httpx.Response(200, content=body))
    assert result.code == 0
    assert result.doc["data"]["imported"] == 1
    assert "sekrit42" not in result.out + result.err
    assert "X-Amz" not in result.out + result.err


def test_url_download_failure_exits_4_and_hides_url(cli: Callable[..., Run]) -> None:
    result = cli("--url", URL, handler=lambda r: httpx.Response(403))
    assert result.code == 4
    assert result.doc["status"] == "error"
    assert "sekrit42" not in result.out + result.err
    assert "X-Amz" not in result.out + result.err


def test_help_lists_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "import-form" in capsys.readouterr().out
