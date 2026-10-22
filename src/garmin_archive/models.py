"""Shared error types and clock helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class GarminArchiveError(Exception):
    """Base class for all library errors, carrying a stable machine-readable code."""

    code: str = "GARMIN_ARCHIVE_ERROR"
    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.details: dict[str, object] = dict(details) if details else {}


class StorageError(GarminArchiveError):
    code = "STORAGE_ERROR"


class DatabaseNotFoundError(StorageError):
    code = "DATABASE_NOT_FOUND"


class NotInitializedError(StorageError):
    code = "NOT_INITIALIZED"


class SchemaVersionError(StorageError):
    code = "SCHEMA_TOO_NEW"


class MigrationChecksumError(StorageError):
    code = "MIGRATION_CHECKSUM_MISMATCH"


class ArchiveLockedError(StorageError):
    code = "ARCHIVE_LOCKED"
    default_retryable = True


class AccountMismatchError(GarminArchiveError):
    code = "ACCOUNT_MISMATCH"


class ApiAuthError(GarminArchiveError):
    """Base for credential and authorization failures against the Garmin API."""

    code = "API_AUTH_ERROR"


class MissingApiKeyError(ApiAuthError):
    code = "MISSING_API_KEY"


class AuthenticationFailedError(ApiAuthError):
    code = "AUTH_FAILED"


class AuthorizationError(ApiAuthError):
    code = "AUTHORIZATION_ERROR"


class ApiTransportError(GarminArchiveError):
    """Base for network, rate-limit and upstream-contract failures."""

    code = "API_TRANSPORT_ERROR"
    default_retryable = True


class TransportError(ApiTransportError):
    code = "TRANSPORT_ERROR"


class RateLimitError(ApiTransportError):
    code = "RATE_LIMITED"


class ContractError(ApiTransportError):
    code = "CONTRACT_ERROR"
    default_retryable = False


class InvalidQueryError(GarminArchiveError):
    code = "INVALID_QUERY"


class QueryTimeoutError(GarminArchiveError):
    code = "QUERY_TIMEOUT"


class Clock(Protocol):
    """Injectable source of the current time."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Clock backed by the host system time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def utc_rfc3339(dt: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` in UTC."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("utc_rfc3339 requires a timezone-aware datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
