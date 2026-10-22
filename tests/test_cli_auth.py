import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from garmin_archive.cli import app, main

runner = CliRunner()
SECRET = "super-secret-key-value"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    monkeypatch.delenv("GARMIN_CONNECT_API_KEY", raising=False)


def test_status_without_key_exits_zero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--env-file", str(tmp_path / "missing.env"), "auth", "status"])
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["data"]["configured"] is False
    assert doc["data"]["source"] is None


def test_status_honours_env_file(tmp_path: Path) -> None:
    env = tmp_path / "custom.env"
    env.write_text(f"GARMIN_API_KEY={SECRET}\n", encoding="utf-8")
    result = runner.invoke(app, ["--env-file", str(env), "auth", "status"])
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["data"] == {"configured": True, "source": str(env)}
    assert SECRET not in result.output


def test_status_reports_env_var_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_API_KEY", SECRET)
    result = runner.invoke(app, ["--env-file", str(tmp_path / "x.env"), "auth", "status"])
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["data"] == {"configured": True, "source": "GARMIN_API_KEY"}
    assert SECRET not in result.output


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_API_KEY", SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, text="tok-123")
        return httpx.Response(200, json=[])

    code = main(
        ["--env-file", str(tmp_path / "x.env"), "auth", "check"], http_client=_client(handler)
    )
    out = capsys.readouterr()
    assert code == 0
    assert [r.method for r in seen] == ["POST", "GET"]
    assert seen[1].url.params["limit"] == "1"
    assert json.loads(out.out)["status"] == "ok"
    assert SECRET not in out.out + out.err
    assert "tok-123" not in out.out + out.err


def test_check_missing_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--env-file", str(tmp_path / "x.env"), "auth", "check"])
    assert code == 3
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "GARMIN_API_KEY" in message and "GARMIN_CONNECT_API_KEY" in message


def test_check_token_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_API_KEY", SECRET)
    code = main(
        ["--env-file", str(tmp_path / "x.env"), "auth", "check"],
        http_client=_client(lambda r: httpx.Response(401)),
    )
    out = capsys.readouterr()
    assert code == 3
    assert SECRET not in out.out + out.err


def test_check_persistent_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_API_KEY", SECRET)
    monkeypatch.setattr("time.sleep", lambda s: None)
    code = main(
        ["--env-file", str(tmp_path / "x.env"), "auth", "check"],
        http_client=_client(lambda r: httpx.Response(503)),
    )
    assert code == 4
    assert SECRET not in capsys.readouterr().out
