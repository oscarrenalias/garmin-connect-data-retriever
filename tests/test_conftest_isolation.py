from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.live
def test_live_marked_test_is_skipped_by_default() -> None:
    assert os.environ.get("GARMIN_LIVE_TESTS") == "1"


def test_env_and_cwd_isolated(tmp_path: Path) -> None:
    assert "GARMIN_API_KEY" not in os.environ
    assert "GARMIN_CONNECT_API_KEY" not in os.environ
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_contract_fixture_dir_exists() -> None:
    assert (Path(__file__).parent / "fixtures" / "contract" / ".gitkeep").is_file()
