import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garmin_archive.cli import app

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
