"""Archive resource keys and schema enum constants (SPEC §5.2)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

KIND_SEARCH_PAGE = "search_page"
KIND_SUMMARY = "summary"
KIND_WORKOUTS = "workouts"
KIND_ORIGINAL_FILE = "original_file"
KIND_IMPORTED_FILE = "imported_file"

# Kinds a resource key may currently be built for. ``imported_file`` is reserved.
RESOURCE_KINDS: tuple[str, ...] = (
    KIND_SEARCH_PAGE,
    KIND_SUMMARY,
    KIND_WORKOUTS,
    KIND_ORIGINAL_FILE,
)
RAW_PAYLOAD_KINDS: tuple[str, ...] = (*RESOURCE_KINDS, KIND_IMPORTED_FILE)

# activity_resources.kind values (0001_archive.sql).
ACTIVITY_RESOURCE_KIND_SEARCH_ITEM = "search_item"
ACTIVITY_RESOURCE_KINDS: tuple[str, ...] = (
    ACTIVITY_RESOURCE_KIND_SEARCH_ITEM,
    KIND_SUMMARY,
    KIND_WORKOUTS,
    KIND_ORIGINAL_FILE,
    KIND_IMPORTED_FILE,
)

# activity_resources.state values.
STATE_PENDING = "pending"
STATE_AVAILABLE = "available"
STATE_UNAVAILABLE = "unavailable"
STATE_FORBIDDEN = "forbidden"
STATE_FAILED = "failed"
RESOURCE_STATES: tuple[str, ...] = (
    STATE_PENDING,
    STATE_AVAILABLE,
    STATE_UNAVAILABLE,
    STATE_FORBIDDEN,
    STATE_FAILED,
)

# activity_archive.availability_status values.
AVAILABILITY_LISTED = "listed"
AVAILABILITY_NOT_FOUND = "not_found"
AVAILABILITY_FORBIDDEN = "forbidden"
AVAILABILITY_STATUSES: tuple[str, ...] = (
    AVAILABILITY_LISTED,
    AVAILABILITY_NOT_FOUND,
    AVAILABILITY_FORBIDDEN,
)

SEARCH_PAGE_KEY_PATTERN = re.compile(r"^search_page(:[a-zA-Z]+=[A-Za-z0-9_.-]+)+$")
_ACTIVITY_KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    KIND_SUMMARY: re.compile(r"^summary:(\d+)$"),
    KIND_WORKOUTS: re.compile(r"^workouts:(\d+)$"),
    KIND_ORIGINAL_FILE: re.compile(r"^original_file:(\d+)$"),
}

_SENSITIVE_PARAM_KEY = re.compile(r"(auth|token|key|secret|password|cookie)", re.IGNORECASE)


def serialize_request_params(params: Mapping[str, str | int | bool] | None) -> str | None:
    """Return compact sorted JSON for request params, or ``None`` when absent.

    Raises ``ValueError`` for credential-like keys. Values are never inspected
    or included in the error message.
    """
    if params is None:
        return None
    for name in params:
        if _SENSITIVE_PARAM_KEY.search(name):
            raise ValueError(f"request param name {name!r} looks like a credential")
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def validate_resource_key(resource_key: str, kind: str, activity_id: str | None) -> None:
    """Raise ``ValueError`` unless the key, kind and activity id are consistent."""
    if kind == KIND_SEARCH_PAGE:
        if activity_id is not None:
            raise ValueError("search_page resource keys must not have an activity_id")
        if not SEARCH_PAGE_KEY_PATTERN.fullmatch(resource_key):
            raise ValueError(f"invalid search_page resource key: {resource_key!r}")
        names: list[str] = []
        for part in resource_key.split(":")[1:]:
            names.append(part.split("=", 1)[0])
        if "start" not in names or "limit" not in names:
            raise ValueError("search_page resource key must include start= and limit=")
        if names != sorted(names):
            raise ValueError("search_page resource key params must be sorted by name")
        return
    pattern = _ACTIVITY_KEY_PATTERNS.get(kind)
    if pattern is None:
        raise ValueError(f"unsupported resource kind: {kind!r}")
    match = pattern.fullmatch(resource_key)
    if match is None:
        raise ValueError(f"invalid {kind} resource key: {resource_key!r}")
    if activity_id != match.group(1):
        raise ValueError(f"activity_id does not match {kind} resource key")


def search_page_key(params: Mapping[str, str | int | bool]) -> str:
    """Build a canonical search_page key with params sorted by name."""
    parts: list[str] = []
    for name in sorted(params):
        value = params[name]
        # bool must be checked first: bool is a subclass of int.
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        parts.append(f"{name}={text}")
    key = ":".join(["search_page", *parts])
    validate_resource_key(key, KIND_SEARCH_PAGE, None)
    return key


def summary_key(activity_id: str) -> str:
    return f"{KIND_SUMMARY}:{activity_id}"


def workouts_key(activity_id: str) -> str:
    return f"{KIND_WORKOUTS}:{activity_id}"


def original_file_key(activity_id: str) -> str:
    return f"{KIND_ORIGINAL_FILE}:{activity_id}"
