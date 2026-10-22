from datetime import UTC, datetime, timedelta, timezone

import pytest

from garmin_archive.models import (
    AccountMismatchError,
    ApiAuthError,
    ApiTransportError,
    ArchiveLockedError,
    AuthenticationFailedError,
    AuthorizationError,
    ContractError,
    DatabaseNotFoundError,
    GarminArchiveError,
    InvalidQueryError,
    MigrationChecksumError,
    MissingApiKeyError,
    NotInitializedError,
    QueryTimeoutError,
    RateLimitError,
    SchemaVersionError,
    StorageError,
    SystemClock,
    TransportError,
    utc_rfc3339,
)


@pytest.mark.parametrize(
    ("cls", "code", "parent"),
    [
        (StorageError, "STORAGE_ERROR", GarminArchiveError),
        (DatabaseNotFoundError, "DATABASE_NOT_FOUND", StorageError),
        (NotInitializedError, "NOT_INITIALIZED", StorageError),
        (SchemaVersionError, "SCHEMA_TOO_NEW", StorageError),
        (MigrationChecksumError, "MIGRATION_CHECKSUM_MISMATCH", StorageError),
        (ArchiveLockedError, "ARCHIVE_LOCKED", StorageError),
        (AccountMismatchError, "ACCOUNT_MISMATCH", GarminArchiveError),
        (ApiAuthError, "API_AUTH_ERROR", GarminArchiveError),
        (MissingApiKeyError, "MISSING_API_KEY", ApiAuthError),
        (AuthenticationFailedError, "AUTH_FAILED", ApiAuthError),
        (AuthorizationError, "AUTHORIZATION_ERROR", ApiAuthError),
        (ApiTransportError, "API_TRANSPORT_ERROR", GarminArchiveError),
        (TransportError, "TRANSPORT_ERROR", ApiTransportError),
        (RateLimitError, "RATE_LIMITED", ApiTransportError),
        (ContractError, "CONTRACT_ERROR", ApiTransportError),
        (InvalidQueryError, "INVALID_QUERY", GarminArchiveError),
        (QueryTimeoutError, "QUERY_TIMEOUT", GarminArchiveError),
    ],
)
def test_error_codes_and_hierarchy(cls: type[GarminArchiveError], code: str, parent: type) -> None:
    err = cls("boom")
    assert err.code == code
    assert isinstance(err, parent)
    assert err.message == "boom"
    assert str(err) == "boom"
    assert err.details == {}


def test_retryable_defaults() -> None:
    assert ArchiveLockedError("x").retryable is True
    assert StorageError("x").retryable is False
    assert AccountMismatchError("x").retryable is False
    assert TransportError("x").retryable is True
    assert RateLimitError("x").retryable is True
    assert ContractError("x").retryable is False
    assert AuthenticationFailedError("x").retryable is False


def test_details_are_copied() -> None:
    details: dict[str, object] = {"path": "a"}
    err = StorageError("x", details=details)
    details["path"] = "b"
    assert err.details == {"path": "a"}


def test_utc_rfc3339_exact() -> None:
    dt = datetime(2024, 3, 5, 7, 8, 9, 123, tzinfo=UTC)
    assert utc_rfc3339(dt) == "2024-03-05T07:08:09.000123Z"


def test_utc_rfc3339_converts_offset() -> None:
    dt = datetime(2024, 3, 5, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert utc_rfc3339(dt) == "2024-03-05T07:00:00.000000Z"


def test_utc_rfc3339_rejects_naive() -> None:
    with pytest.raises(ValueError):
        utc_rfc3339(datetime(2024, 1, 1))


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
