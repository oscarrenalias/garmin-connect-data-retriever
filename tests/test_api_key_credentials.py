from __future__ import annotations

from pathlib import Path

import pytest

from garmin_archive.credentials import find_api_key, parse_dotenv, require_api_key
from garmin_archive.models import MissingApiKeyError


def test_primary_wins_over_secondary() -> None:
    src = require_api_key({"GARMIN_API_KEY": "a", "GARMIN_CONNECT_API_KEY": "b"}, Path("x"))
    assert (src.key, src.variable, src.env_file) == ("a", "GARMIN_API_KEY", None)


def test_secondary_used_when_primary_absent() -> None:
    src = require_api_key({"GARMIN_CONNECT_API_KEY": "b"}, Path("x"))
    assert src.variable == "GARMIN_CONNECT_API_KEY"


def test_environ_wins_over_dotenv(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("GARMIN_API_KEY=file\n")
    src = require_api_key({"GARMIN_CONNECT_API_KEY": "env"}, f)
    assert src.key == "env" and src.env_file is None


def test_custom_env_file_honoured(tmp_path: Path) -> None:
    f = tmp_path / "custom.env"
    f.write_text("GARMIN_CONNECT_API_KEY=file\n")
    src = require_api_key({}, f)
    assert (src.key, src.variable, src.env_file) == ("file", "GARMIN_CONNECT_API_KEY", f)


def test_missing_file_yields_none(tmp_path: Path) -> None:
    assert find_api_key({}, tmp_path / "nope.env") is None


def test_parse_quotes_and_comments() -> None:
    text = (
        "# comment\n\nA=plain # trailing\nB=\"dq # kept\"\nC='sq' # note\nD=no#hash\nexport E=x\n"
    )
    assert parse_dotenv(text) == {
        "A": "plain",
        "B": "dq # kept",
        "C": "sq",
        "D": "no#hash",
        "E": "x",
    }


def test_missing_key_raises_naming_both(tmp_path: Path) -> None:
    with pytest.raises(MissingApiKeyError) as ei:
        require_api_key({}, tmp_path / ".env")
    assert "GARMIN_API_KEY" in str(ei.value)
    assert "GARMIN_CONNECT_API_KEY" in str(ei.value)


def test_key_not_in_repr() -> None:
    src = require_api_key({"GARMIN_API_KEY": "s3cret-value"}, Path("x"))
    assert "s3cret-value" not in repr(src)
    assert "s3cret-value" not in str(src)
    assert "s3cret-value" not in src.origin
