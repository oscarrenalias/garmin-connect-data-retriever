"""Archive resource keys and schema enum constants (SPEC §5.2)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import TypeVar

from garmin_archive.models import Clock, StorageError, utc_rfc3339
from garmin_archive.storage import connection

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


def _is_text_media_type(media_type: str) -> bool:
    return (
        media_type == "application/json"
        or media_type.startswith("text/")
        or (media_type.endswith("+json") and "/" in media_type)
    )


def _derive_body_text(content_type: str | None, body: bytes) -> tuple[str | None, str | None]:
    """Return ``(body_text, encoding)`` for a response body.

    Only JSON and ``text/*`` media types are decoded. Undecodable bodies and
    unknown charsets yield ``(None, None)``. The body is never parsed as JSON.
    """
    if not content_type:
        return None, None
    media_type, *params = content_type.split(";")
    if not _is_text_media_type(media_type.strip().lower()):
        return None, None
    charset = "utf-8"
    for param in params:
        name, sep, value = param.partition("=")
        if sep and name.strip().lower() == "charset":
            value = value.strip().strip('"').strip()
            if value:
                charset = value.lower()
            break
    try:
        return body.decode(charset), charset
    except (UnicodeDecodeError, LookupError):
        return None, None


def _sha256_and_size(body: bytes) -> tuple[str, int]:
    """Return the lowercase hex SHA-256 digest and byte count of ``body``."""
    return hashlib.sha256(body).hexdigest(), len(body)


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


_ACTIVITY_ID_PATTERN = re.compile(r"[0-9]+")
_T = TypeVar("_T")


def _validate_activity_id(activity_id: str) -> None:
    if not isinstance(activity_id, str) or not _ACTIVITY_ID_PATTERN.fullmatch(activity_id):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("activity_id must be a string of ASCII digits")


def _validate_availability(status: str) -> None:
    if status not in AVAILABILITY_STATUSES:
        raise ValueError(f"invalid availability_status: {status!r}")


class ArchiveWriter:
    """Write operations on the archive tables.

    Every public method validates its arguments in Python before any write, runs
    in exactly one transaction, and converts ``sqlite3.Error`` into
    ``StorageError`` (chained via ``__cause__``) after rolling back.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def _now(self) -> str:
        return utc_rfc3339(self._clock.now())

    def _run(self, work: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run ``work`` in one transaction; map ``sqlite3.Error`` to ``StorageError``."""
        try:
            with connection.transaction(self._conn) as conn:
                return work(conn)
        except sqlite3.Error as exc:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise StorageError(f"Archive write failed: {exc}") from exc

    def upsert_activity(
        self,
        *,
        activity_id: str,
        owner_profile_id: str | None,
        parent_activity_id: str | None,
        type_key: str | None,
        availability_status: str = AVAILABILITY_LISTED,
    ) -> bool:
        """Insert or update an activity row; return ``True`` when inserted."""
        _validate_activity_id(activity_id)
        _validate_availability(availability_status)
        now = self._now()

        def work(conn: sqlite3.Connection) -> bool:
            exists = (
                conn.execute(
                    "SELECT 1 FROM activity_archive WHERE activity_id = ?", (activity_id,)
                ).fetchone()
                is not None
            )
            if exists:
                conn.execute(
                    "UPDATE activity_archive SET owner_profile_id = ?, parent_activity_id = ?,"
                    " type_key = ?, availability_status = ?, last_seen_at_utc = ?"
                    " WHERE activity_id = ?",
                    (
                        owner_profile_id,
                        parent_activity_id,
                        type_key,
                        availability_status,
                        now,
                        activity_id,
                    ),
                )
                return False
            conn.execute(
                "INSERT INTO activity_archive (activity_id, owner_profile_id,"
                " parent_activity_id, type_key, first_seen_at_utc, last_seen_at_utc,"
                " availability_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    activity_id,
                    owner_profile_id,
                    parent_activity_id,
                    type_key,
                    now,
                    now,
                    availability_status,
                ),
            )
            return True

        return self._run(work)

    def mark_summary_check(self, *, activity_id: str, availability_status: str) -> None:
        """Record a summary check outcome; never deletes the row."""
        _validate_activity_id(activity_id)
        _validate_availability(availability_status)
        now = self._now()

        def work(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "UPDATE activity_archive SET last_summary_check_at_utc = ?,"
                " availability_status = ? WHERE activity_id = ?",
                (now, availability_status, activity_id),
            )
            if cur.rowcount != 1:
                raise StorageError(f"Activity {activity_id} not found in archive")

        self._run(work)
