from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from garmin_archive.cli import main
from garmin_archive.storage.lock import writer_lock


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any]]:
    code = main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_init_creates_then_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    code, doc = _run(capsys, ["--db", str(db), "init"])
    assert code == 0
    assert doc["status"] == "ok"
    assert doc["data"]["created"] is True
    assert doc["data"]["schema_version"] == 1
    assert doc["data"]["db_path"] == str(db.resolve())
    code, doc = _run(capsys, ["--db", str(db), "init"])
    assert code == 0
    assert doc["data"]["created"] is False
    assert doc["data"]["applied_migrations"] == []


def test_schema_too_new(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "a.db"
    assert _run(capsys, ["--db", str(db), "init"])[0] == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at_utc, checksum) "
        "VALUES (99, '2026-01-01T00:00:00.000000Z', 'x')"
    )
    conn.commit()
    conn.close()
    code, doc = _run(capsys, ["--db", str(db), "init"])
    assert code == 5
    assert doc["error"]["code"] == "SCHEMA_TOO_NEW"


def test_unknown_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = _run(capsys, ["--db", str(tmp_path / "a.db"), "init", "--bogus"])
    assert code == 2
    assert doc["error"]["code"] == "INVALID_ARGUMENT"


def test_locked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "a.db"
    with writer_lock(db.resolve()):
        code, doc = _run(capsys, ["--db", str(db), "init"])
    assert code == 5
    assert doc["error"]["code"] == "ARCHIVE_LOCKED"
    assert doc["error"]["retryable"] is True


def test_corrupt_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "a.db"
    db.write_bytes(bytes(range(256)) * 64)
    code, doc = _run(capsys, ["--db", str(db), "init"])
    assert code == 5
    assert doc["error"]["code"] == "STORAGE_ERROR"


def test_version_and_help_plain_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert "{" not in capsys.readouterr().out
    assert main(["--help"]) == 0
    assert "--db" in capsys.readouterr().out
