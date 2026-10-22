from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from garmin_archive.storage.migrations import DirectoryMigrations, PackagedMigrations


def _write(directory: Path, name: str, content: str) -> bytes:
    data = content.encode("utf-8")
    (directory / name).write_bytes(data)
    return data


def test_loads_sorted_with_version_name_checksum(tmp_path: Path) -> None:
    second = _write(tmp_path, "0002_more.sql", "CREATE TABLE b (x INTEGER);\n")
    first = _write(tmp_path, "0001_init.sql", "CREATE TABLE a (x INTEGER);\n")
    (tmp_path / "notes.txt").write_text("ignored")

    migrations = DirectoryMigrations(tmp_path).load()

    assert [m.version for m in migrations] == [1, 2]
    assert [m.name for m in migrations] == ["init", "more"]
    assert migrations[0].checksum == hashlib.sha256(first).hexdigest()
    assert migrations[1].checksum == hashlib.sha256(second).hexdigest()
    assert migrations[0].sql == first.decode()


def test_duplicate_version_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a (x);")
    _write(tmp_path, "1_b.sql", "CREATE TABLE b (x);")
    with pytest.raises(ValueError):
        DirectoryMigrations(tmp_path).load()


def test_non_numeric_prefix_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "abcd_init.sql", "CREATE TABLE a (x);")
    with pytest.raises(ValueError):
        DirectoryMigrations(tmp_path).load()


@pytest.mark.parametrize("keyword", ["COMMIT", "begin", "Rollback"])
def test_transaction_keywords_rejected(tmp_path: Path, keyword: str) -> None:
    _write(tmp_path, "0001_a.sql", f"CREATE TABLE a (x);\n{keyword};\n")
    with pytest.raises(ValueError):
        DirectoryMigrations(tmp_path).load()


def test_keyword_in_line_comment_allowed(tmp_path: Path) -> None:
    _write(tmp_path, "0001_a.sql", "-- do not COMMIT here\nCREATE TABLE a (x); -- BEGIN\n")
    assert [m.version for m in DirectoryMigrations(tmp_path).load()] == [1]


def test_keyword_as_substring_allowed(tmp_path: Path) -> None:
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a (committed INTEGER, beginning TEXT);\n")
    assert len(DirectoryMigrations(tmp_path).load()) == 1


def test_packaged_migrations_loads_sorted_unique() -> None:
    versions = [m.version for m in PackagedMigrations().load()]
    assert versions == sorted(set(versions))


def test_hatch_config_includes_migration_sql() -> None:
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "src/garmin_archive/storage/migrations/*.sql" in wheel["include"]
