from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("GARMIN_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live tests require GARMIN_LIVE_TESTS=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolate_env_and_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ensure the real API key and the repo's ./.env can never be resolved."""
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    monkeypatch.delenv("GARMIN_CONNECT_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
