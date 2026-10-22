from __future__ import annotations

from pathlib import Path

import pytest

from garmin_archive.devtools.discovery.credentials import (
    MissingApiKeyError,
    SecretStr,
    load_api_key,
    parse_dotenv,
)

SECRET = "s3cr3t-value-123"


def test_env_primary_variable(tmp_path: Path) -> None:
    key = load_api_key({"GARMIN_API_KEY": SECRET}, None, tmp_path)
    assert key.get_secret_value() == SECRET


def test_env_secondary_variable(tmp_path: Path) -> None:
    key = load_api_key({"GARMIN_CONNECT_API_KEY": SECRET}, None, tmp_path)
    assert key.get_secret_value() == SECRET


def test_env_primary_beats_secondary(tmp_path: Path) -> None:
    env = {"GARMIN_API_KEY": "one", "GARMIN_CONNECT_API_KEY": "two"}
    assert load_api_key(env, None, tmp_path).get_secret_value() == "one"


def test_file_precedence_between_variables(tmp_path: Path) -> None:
    f = tmp_path / "x.env"
    f.write_text("GARMIN_CONNECT_API_KEY=two\nGARMIN_API_KEY=one\n")
    assert load_api_key({}, f, tmp_path).get_secret_value() == "one"


def test_env_beats_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GARMIN_API_KEY=fromfile\n")
    key = load_api_key({"GARMIN_API_KEY": "fromenv"}, None, tmp_path)
    assert key.get_secret_value() == "fromenv"


def test_default_cwd_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GARMIN_API_KEY=fromcwd\n")
    assert load_api_key({}, None, tmp_path).get_secret_value() == "fromcwd"


def test_explicit_env_file_beats_cwd_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GARMIN_API_KEY=fromcwd\n")
    other = tmp_path / "other.env"
    other.write_text("GARMIN_API_KEY=explicit\n")
    assert load_api_key({}, other, tmp_path).get_secret_value() == "explicit"


def test_explicit_env_file_does_not_fall_back_to_cwd(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GARMIN_API_KEY=fromcwd\n")
    with pytest.raises(MissingApiKeyError):
        load_api_key({}, tmp_path / "missing.env", tmp_path)


def test_missing_file_and_env_raises(tmp_path: Path) -> None:
    with pytest.raises(MissingApiKeyError):
        load_api_key({}, None, tmp_path)


def test_empty_env_value_ignored(tmp_path: Path) -> None:
    with pytest.raises(MissingApiKeyError):
        load_api_key({"GARMIN_API_KEY": ""}, None, tmp_path)


def test_missing_message_names_both_variables() -> None:
    msg = str(MissingApiKeyError())
    assert "GARMIN_API_KEY" in msg
    assert "GARMIN_CONNECT_API_KEY" in msg


def test_parse_dotenv_syntax() -> None:
    text = (
        "# comment\n"
        "\n"
        "A=plain\n"
        'B="double quoted # not comment"\n'
        "C='single quoted'\n"
        "export D=exported\n"
        "E=value # trailing comment\n"
        "F=a#b\n"
        "not a pair\n"
        "=novalue\n"
    )
    assert parse_dotenv(text) == {
        "A": "plain",
        "B": "double quoted # not comment",
        "C": "single quoted",
        "D": "exported",
        "E": "value",
        "F": "a#b",
    }


def test_quoted_and_export_in_file(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(f"export GARMIN_API_KEY='{SECRET}'  # note\n")
    assert load_api_key({}, None, tmp_path).get_secret_value() == SECRET


def test_secretstr_never_reveals_value() -> None:
    s = SecretStr(SECRET)
    assert SECRET not in repr(s)
    assert SECRET not in str(s)
    assert SECRET not in f"{s} {s!r}"
    assert s.get_secret_value() == SECRET


def test_missing_error_does_not_contain_values(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"OTHER={SECRET}\n")
    with pytest.raises(MissingApiKeyError) as ei:
        load_api_key({"UNRELATED": SECRET}, None, tmp_path)
    assert SECRET not in str(ei.value)
    assert SECRET not in repr(ei.value)


def test_cli_has_no_api_key_option() -> None:
    from typer.testing import CliRunner

    from garmin_archive.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--api-key" not in result.output
