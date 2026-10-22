from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest

from garmin_archive.config import ArchiveConfig, resolve_db_path


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda *a, **k: str(d))
    return d


def test_defaults() -> None:
    cfg = ArchiveConfig()
    assert cfg.db_path is None
    assert cfg.busy_timeout_ms == 5000


def test_explicit_beats_env_beats_default(tmp_path: Path, data_dir: Path) -> None:
    env = {"GARMIN_ARCHIVE_DB": str(tmp_path / "env.db")}
    explicit = tmp_path / "explicit.db"
    assert resolve_db_path(ArchiveConfig(db_path=explicit), env) == explicit.resolve()
    assert resolve_db_path(ArchiveConfig(), env) == (tmp_path / "env.db").resolve()
    assert resolve_db_path(ArchiveConfig(), {}) == (data_dir / "archive.db").resolve()


def test_empty_env_falls_through(data_dir: Path) -> None:
    assert (
        resolve_db_path(ArchiveConfig(), {"GARMIN_ARCHIVE_DB": ""})
        == (data_dir / "archive.db").resolve()
    )


def test_relative_path_becomes_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = resolve_db_path(ArchiveConfig(db_path=Path("rel.db")), {})
    assert result.is_absolute()
    assert result == (tmp_path / "rel.db").resolve()


def test_tilde_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = resolve_db_path(ArchiveConfig(), {"GARMIN_ARCHIVE_DB": "~/x.db"})
    assert result == (tmp_path / "x.db").resolve()


def test_config_is_frozen() -> None:
    with pytest.raises(AttributeError):
        ArchiveConfig().busy_timeout_ms = 1  # type: ignore[misc]
