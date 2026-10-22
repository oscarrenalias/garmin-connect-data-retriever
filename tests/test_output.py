from __future__ import annotations

import io
import json

import pytest
from typer._click import exceptions as click  # pyright: ignore[reportMissingModuleSource]

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
    TransportError,
)
from garmin_archive.output import emit, error, exit_code_for, success


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AccountMismatchError("x"), 3),
        (ApiAuthError("x"), 3),
        (MissingApiKeyError("x"), 3),
        (AuthenticationFailedError("x"), 3),
        (AuthorizationError("x"), 3),
        (ApiTransportError("x"), 4),
        (TransportError("x"), 4),
        (RateLimitError("x"), 4),
        (ContractError("x"), 4),
        (StorageError("x"), 5),
        (DatabaseNotFoundError("x"), 5),
        (NotInitializedError("x"), 5),
        (SchemaVersionError("x"), 5),
        (MigrationChecksumError("x"), 5),
        (ArchiveLockedError("x"), 5),
        (InvalidQueryError("x"), 2),
        (QueryTimeoutError("x"), 8),
        (GarminArchiveError("x"), 1),
        (click.UsageError("x"), 2),
        (click.BadParameter("x"), 2),
        (RuntimeError("x"), 1),
    ],
)
def test_exit_code_for(exc: BaseException, expected: int) -> None:
    assert exit_code_for(exc) == expected


def test_success_omits_empty_warnings() -> None:
    expected = {"status": "ok", "data": [1], "meta": {"output_version": 1}}
    assert success([1]) == expected
    assert success([1], warnings=None) == expected
    assert success([1], warnings=[]) == expected


def test_success_includes_warnings() -> None:
    doc = success({"a": 1}, warnings=["w"])
    assert doc["warnings"] == ["w"]
    assert doc["meta"] == {"output_version": 1}


def test_error_envelope_defaults() -> None:
    assert error("C", "msg") == {
        "status": "error",
        "error": {"code": "C", "message": "msg", "retryable": False, "details": {}},
    }


def test_error_default_details_is_empty_dict_and_not_shared() -> None:
    first = error("X", "m")
    assert first["error"]["details"] == {}
    first["error"]["details"]["k"] = "v"
    assert error("X", "m")["error"]["details"] == {}


def test_error_envelope_with_details() -> None:
    doc = error("C", "m", retryable=True, details={"k": "v"})
    assert doc["error"]["retryable"] is True
    assert doc["error"]["details"] == {"k": "v"}


def test_emit_single_document_with_newline_and_unicode() -> None:
    stream = io.StringIO()
    emit(success({"name": "Zürich 走"}), stream)
    text = stream.getvalue()
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert "Zürich 走" in text
    assert json.loads(text)["data"]["name"] == "Zürich 走"
