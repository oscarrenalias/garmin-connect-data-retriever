"""Migration model and sources (SPEC §4: loading)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

_FILENAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")
_FORBIDDEN_RE = re.compile(r"\b(BEGIN|COMMIT|ROLLBACK)\b", re.IGNORECASE)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


class MigrationSource(Protocol):
    def load(self) -> Sequence[Migration]:
        """Return migrations sorted by version with unique versions."""
        ...


def _build_migration(filename: str, data: bytes) -> Migration:
    match = _FILENAME_RE.match(filename)
    if match is None:
        raise ValueError(f"Migration file name must look like NNNN_<slug>.sql: {filename}")
    sql = data.decode("utf-8")
    if _FORBIDDEN_RE.search(_LINE_COMMENT_RE.sub("", sql)):
        raise ValueError(f"Migration {filename} must not contain BEGIN, COMMIT or ROLLBACK")
    return Migration(
        version=int(match.group(1)),
        name=match.group(2),
        sql=sql,
        checksum=hashlib.sha256(data).hexdigest(),
    )


def _finalize(migrations: list[Migration]) -> list[Migration]:
    migrations.sort(key=lambda m: m.version)
    for previous, current in zip(migrations, migrations[1:], strict=False):
        if previous.version == current.version:
            raise ValueError(f"Duplicate migration version: {current.version}")
    return migrations


class PackagedMigrations:
    """Migrations shipped in ``garmin_archive.storage.migrations``."""

    def load(self) -> Sequence[Migration]:
        package = resources.files("garmin_archive.storage.migrations")
        found = [
            _build_migration(entry.name, entry.read_bytes())
            for entry in package.iterdir()
            if entry.is_file() and entry.name.endswith(".sql")
        ]
        return _finalize(found)


class DirectoryMigrations:
    """Migrations loaded from a filesystem directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Sequence[Migration]:
        found = [
            _build_migration(entry.name, entry.read_bytes())
            for entry in self._path.iterdir()
            if entry.is_file() and entry.name.endswith(".sql")
        ]
        return _finalize(found)
