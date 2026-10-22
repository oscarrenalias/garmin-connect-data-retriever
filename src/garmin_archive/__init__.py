"""Local-first archive of Garmin Connect activities."""

from garmin_archive._version import __version__
from garmin_archive.client import ArchiveClient, InitResult
from garmin_archive.config import ArchiveConfig
from garmin_archive.models import (
    AccountMismatchError,
    ArchiveLockedError,
    DatabaseNotFoundError,
    GarminArchiveError,
    MigrationChecksumError,
    NotInitializedError,
    SchemaVersionError,
    StorageError,
)

__all__ = [
    "AccountMismatchError",
    "ArchiveClient",
    "ArchiveConfig",
    "ArchiveLockedError",
    "DatabaseNotFoundError",
    "GarminArchiveError",
    "InitResult",
    "MigrationChecksumError",
    "NotInitializedError",
    "SchemaVersionError",
    "StorageError",
    "__version__",
]
