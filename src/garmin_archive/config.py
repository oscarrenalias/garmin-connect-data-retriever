"""Archive configuration and database path resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import platformdirs

DB_ENV_VAR = "GARMIN_ARCHIVE_DB"


@dataclass(frozen=True)
class ArchiveConfig:
    """Settings for opening the archive database."""

    db_path: Path | None = None
    busy_timeout_ms: int = 5000


def resolve_db_path(config: ArchiveConfig, environ: Mapping[str, str]) -> Path:
    """Resolve the database path: explicit config, then environment, then the user data dir."""
    if config.db_path is not None:
        path = Path(config.db_path)
    elif environ.get(DB_ENV_VAR):
        path = Path(environ[DB_ENV_VAR])
    else:
        path = Path(platformdirs.user_data_dir("garmin-archive", appauthor=False)) / "archive.db"
    return path.expanduser().resolve()
