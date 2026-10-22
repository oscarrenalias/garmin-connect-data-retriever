"""JSON output envelopes and exit-code mapping (SPEC §9)."""

from __future__ import annotations

import json
from typing import Any, TextIO

from garmin_archive.models import AccountMismatchError, GarminArchiveError, StorageError

UsageError: type[Exception]
try:
    from click import UsageError  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
except ImportError:  # typer >= 0.27 vendors click and no longer depends on it
    from typer._click.exceptions import UsageError  # pyright: ignore[reportAttributeAccessIssue]

OUTPUT_VERSION = 1


def success(data: Any, *, warnings: list[Any] | None = None) -> dict[str, Any]:
    """Build a success envelope; ``warnings`` is included only when non-empty."""
    document: dict[str, Any] = {
        "status": "ok",
        "data": data,
        "meta": {"output_version": OUTPUT_VERSION},
    }
    if warnings:
        document["warnings"] = list(warnings)
    return document


def error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an error envelope."""
    return {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": {} if details is None else details,
        },
    }


def exit_code_for(exc: BaseException) -> int:
    """Map an exception to the CLI process exit code."""
    if isinstance(exc, AccountMismatchError):
        return 3
    if isinstance(exc, StorageError):
        return 5
    if isinstance(exc, GarminArchiveError):
        return 1
    if isinstance(exc, UsageError):
        return 2
    return 1


def emit(document: dict[str, Any], stream: TextIO) -> None:
    """Write exactly one JSON document followed by a newline."""
    stream.write(json.dumps(document, ensure_ascii=False))
    stream.write("\n")
