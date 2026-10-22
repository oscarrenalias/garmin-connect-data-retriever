"""Raw archive resource keys, schema enum constants, writer and reader (SPEC §5.1, §5.2, §6.2)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar, cast

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

# Per-activity kinds that sync can mark pending.
_PENDING_KINDS: tuple[str, ...] = (KIND_SUMMARY, KIND_WORKOUTS, KIND_ORIGINAL_FILE)

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
    """Build the ``summary:<activity_id>`` resource key."""
    return f"{KIND_SUMMARY}:{activity_id}"


def workouts_key(activity_id: str) -> str:
    """Build the ``workouts:<activity_id>`` resource key."""
    return f"{KIND_WORKOUTS}:{activity_id}"


def original_file_key(activity_id: str) -> str:
    """Build the ``original_file:<activity_id>`` resource key."""
    return f"{KIND_ORIGINAL_FILE}:{activity_id}"


_ACTIVITY_ID_PATTERN = re.compile(r"[0-9]+")
_SEARCH_ITEM_POINTER = re.compile(r"/[0-9]+")
_FAILURE_STATES = (STATE_UNAVAILABLE, STATE_FORBIDDEN, STATE_FAILED)
_T = TypeVar("_T")


def _validate_activity_id(activity_id: str) -> None:
    if not isinstance(activity_id, str) or not _ACTIVITY_ID_PATTERN.fullmatch(activity_id):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("activity_id must be a string of ASCII digits")


def _validate_availability(status: str) -> None:
    if status not in AVAILABILITY_STATUSES:
        raise ValueError(f"invalid availability_status: {status!r}")


def _validate_resource_state(state: str) -> None:
    if state not in RESOURCE_STATES:
        raise ValueError(f"invalid resource_state: {state!r}")


@dataclass(frozen=True)
class RecordResult:
    """Outcome of ``ArchiveWriter.record_response``."""

    payload_id: int
    fetch_event_id: int
    is_new_payload: bool
    sha256: str


class ArchiveWriter:
    """Write operations on the archive tables.

    Every public method validates its arguments in Python before any write, runs
    in exactly one transaction, and converts ``sqlite3.Error`` into
    ``StorageError`` (chained via ``__cause__``) after rolling back.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock
        self._batch_depth = 0

    def _now(self) -> str:
        return utc_rfc3339(self._clock.now())

    @contextmanager
    def batch(self) -> Generator[None]:
        """Group writer calls into one transaction; nested batches join the outermost.

        Any exception rolls back the whole batch; ``sqlite3.Error`` becomes ``StorageError``.
        """
        if self._batch_depth > 0:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
            return
        try:
            with connection.transaction(self._conn):
                self._batch_depth = 1
                try:
                    yield
                finally:
                    self._batch_depth = 0
        except sqlite3.Error as exc:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise StorageError(f"Archive write failed: {exc}") from exc

    def _run(self, work: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run ``work`` in one transaction (or the enclosing batch); map ``sqlite3.Error``."""
        if self._batch_depth > 0:
            try:
                return work(self._conn)
            except sqlite3.Error as exc:
                raise StorageError(f"Archive write failed: {exc}") from exc
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
        """Insert or update an activity row; return ``True`` when inserted.

        ``first_seen_at_utc`` is set on insert only; ``last_seen_at_utc`` is refreshed on
        every call. Raises ``ValueError`` for a non-digit id or unknown availability status.
        """
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

    def update_activity_classification(
        self, *, activity_id: str, type_key: str | None, parent_activity_id: str | None
    ) -> None:
        """Set ``type_key`` and ``parent_activity_id`` on an existing activity row."""
        _validate_activity_id(activity_id)

        def work(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "UPDATE activity_archive SET type_key = ?, parent_activity_id = ?"
                " WHERE activity_id = ?",
                (type_key, parent_activity_id, activity_id),
            )
            if cur.rowcount != 1:
                raise StorageError(f"Activity {activity_id} not found in archive")

        self._run(work)

    def mark_pending(self, *, activity_id: str, kinds: Iterable[str]) -> None:
        """Set summary/workouts/original_file resources to ``pending``.

        Missing rows are created; existing rows keep ``current_payload_id`` and all archived
        payloads. Invalid ids or kinds raise ``ValueError`` before any write.
        """
        _validate_activity_id(activity_id)
        kind_list = list(dict.fromkeys(kinds))
        for kind in kind_list:
            if kind not in _PENDING_KINDS:
                raise ValueError(f"invalid kind: {kind!r}")

        def work(conn: sqlite3.Connection) -> None:
            if (
                conn.execute(
                    "SELECT 1 FROM activity_archive WHERE activity_id = ?", (activity_id,)
                ).fetchone()
                is None
            ):
                raise StorageError(f"Activity {activity_id} not found in archive")
            for kind in kind_list:
                conn.execute(
                    "INSERT INTO activity_resources (activity_id, kind, state)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT (activity_id, kind) DO UPDATE SET state = excluded.state",
                    (activity_id, kind, STATE_PENDING),
                )

        self._run(work)

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

    def record_response(
        self,
        *,
        resource_key: str,
        kind: str,
        activity_id: str | None,
        sync_run_id: int | None,
        method: str,
        path: str,
        request_params: Mapping[str, str | int | bool] | None,
        http_status: int | None,
        content_type: str | None,
        body: bytes,
        etag: str | None = None,
        last_modified: str | None = None,
        resource_state: str = STATE_AVAILABLE,
        json_pointer: str | None = None,
    ) -> RecordResult:
        """Store a response body (deduplicated) and its fetch event atomically.

        The body is stored byte-for-byte; an identical ``(resource_key, sha256)`` reuses the
        existing payload row (``is_new_payload=False``), while changed content adds a new
        version. A fetch event is always appended and, for activity-scoped kinds, the
        ``activity_resources`` row is pointed at the payload. Raises ``ValueError`` for
        invalid keys, kinds, states or credential-like request params (before any write)
        and ``StorageError`` if the activity is not archived.
        """
        if kind == KIND_IMPORTED_FILE:
            raise ValueError("imported_file payloads cannot be recorded")
        _validate_resource_state(resource_state)
        validate_resource_key(resource_key, kind, activity_id)
        params_json = serialize_request_params(request_params)
        sha256, byte_count = _sha256_and_size(body)
        body_text, encoding = _derive_body_text(content_type, body)
        now = self._now()

        def work(conn: sqlite3.Connection) -> RecordResult:
            if kind != KIND_SEARCH_PAGE:
                row = conn.execute(
                    "SELECT 1 FROM activity_archive WHERE activity_id = ?", (activity_id,)
                ).fetchone()
                if row is None:
                    raise StorageError(f"Activity {activity_id} not found in archive")
            existing = conn.execute(
                "SELECT id FROM raw_payloads WHERE resource_key = ? AND sha256 = ?",
                (resource_key, sha256),
            ).fetchone()
            if existing is not None:
                payload_id = int(existing[0])
                is_new = False
            else:
                inserted = conn.execute(
                    "INSERT INTO raw_payloads (resource_key, activity_id, kind, content_type,"
                    " encoding, body_text, body_blob, sha256, byte_count, first_seen_at_utc)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource_key,
                        activity_id if kind != KIND_SEARCH_PAGE else None,
                        kind,
                        content_type,
                        encoding,
                        body_text,
                        body,
                        sha256,
                        byte_count,
                        now,
                    ),
                ).lastrowid
                assert inserted is not None
                payload_id = inserted
                is_new = True
            fetch_event_id = conn.execute(
                "INSERT INTO fetch_events (sync_run_id, resource_key, method, path,"
                " request_params_json, fetched_at_utc, http_status, payload_id, etag,"
                " last_modified, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    sync_run_id,
                    resource_key,
                    method,
                    path,
                    params_json,
                    now,
                    http_status,
                    payload_id,
                    etag,
                    last_modified,
                ),
            ).lastrowid
            assert fetch_event_id is not None
            if kind != KIND_SEARCH_PAGE:
                conn.execute(
                    "INSERT INTO activity_resources (activity_id, kind, current_payload_id,"
                    " json_pointer, state, last_attempt_at_utc, last_success_at_utc,"
                    " next_retry_at_utc, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)"
                    " ON CONFLICT (activity_id, kind) DO UPDATE SET"
                    " current_payload_id = excluded.current_payload_id,"
                    " json_pointer = excluded.json_pointer, state = excluded.state,"
                    " last_attempt_at_utc = excluded.last_attempt_at_utc,"
                    " last_success_at_utc = excluded.last_success_at_utc,"
                    " next_retry_at_utc = NULL, error_code = NULL",
                    (activity_id, kind, payload_id, json_pointer, resource_state, now, now),
                )
            return RecordResult(payload_id, fetch_event_id, is_new, sha256)

        return self._run(work)

    def record_failure(
        self,
        *,
        resource_key: str,
        kind: str,
        activity_id: str | None,
        sync_run_id: int | None,
        method: str,
        path: str,
        request_params: Mapping[str, str | int | bool] | None,
        http_status: int | None,
        error_code: str,
        resource_state: str,
        next_retry_at_utc: str | None = None,
    ) -> int:
        """Record a failed fetch (no body stored); return the fetch event id.

        Appends a fetch event with ``error_code`` and updates the resource state
        (``unavailable``, ``forbidden`` or ``failed``) and retry time. Previously archived
        payloads and ``current_payload_id`` are left untouched.
        """
        if resource_state not in _FAILURE_STATES:
            raise ValueError(f"invalid failure resource_state: {resource_state!r}")
        if kind == KIND_IMPORTED_FILE:
            raise ValueError("imported_file failures cannot be recorded")
        validate_resource_key(resource_key, kind, activity_id)
        params_json = serialize_request_params(request_params)
        now = self._now()

        def work(conn: sqlite3.Connection) -> int:
            if kind != KIND_SEARCH_PAGE:
                row = conn.execute(
                    "SELECT 1 FROM activity_archive WHERE activity_id = ?", (activity_id,)
                ).fetchone()
                if row is None:
                    raise StorageError(f"Activity {activity_id} not found in archive")
            fetch_event_id = conn.execute(
                "INSERT INTO fetch_events (sync_run_id, resource_key, method, path,"
                " request_params_json, fetched_at_utc, http_status, payload_id, etag,"
                " last_modified, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
                (
                    sync_run_id,
                    resource_key,
                    method,
                    path,
                    params_json,
                    now,
                    http_status,
                    error_code,
                ),
            ).lastrowid
            assert fetch_event_id is not None
            if kind != KIND_SEARCH_PAGE:
                conn.execute(
                    "INSERT INTO activity_resources (activity_id, kind, current_payload_id,"
                    " json_pointer, state, last_attempt_at_utc, last_success_at_utc,"
                    " next_retry_at_utc, error_code) VALUES (?, ?, NULL, NULL, ?, ?, NULL, ?, ?)"
                    " ON CONFLICT (activity_id, kind) DO UPDATE SET"
                    " state = excluded.state,"
                    " last_attempt_at_utc = excluded.last_attempt_at_utc,"
                    " next_retry_at_utc = excluded.next_retry_at_utc,"
                    " error_code = excluded.error_code",
                    (activity_id, kind, resource_state, now, next_retry_at_utc, error_code),
                )
            return fetch_event_id

        return self._run(work)

    def record_search_item(
        self, *, activity_id: str, search_page_payload_id: int, json_pointer: str
    ) -> None:
        """Point an activity's ``search_item`` resource at an item in a search page payload."""
        _validate_activity_id(activity_id)
        if not isinstance(json_pointer, str) or not _SEARCH_ITEM_POINTER.fullmatch(json_pointer):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("json_pointer must match ^/\\d+$")
        now = self._now()

        def work(conn: sqlite3.Connection) -> None:
            if (
                conn.execute(
                    "SELECT 1 FROM activity_archive WHERE activity_id = ?", (activity_id,)
                ).fetchone()
                is None
            ):
                raise StorageError(f"Activity {activity_id} not found in archive")
            if (
                conn.execute(
                    "SELECT 1 FROM raw_payloads WHERE id = ? AND kind = ?",
                    (search_page_payload_id, KIND_SEARCH_PAGE),
                ).fetchone()
                is None
            ):
                raise StorageError(f"Payload {search_page_payload_id} is not a search_page payload")
            conn.execute(
                "INSERT INTO activity_resources (activity_id, kind, current_payload_id,"
                " json_pointer, state, last_attempt_at_utc, last_success_at_utc,"
                " next_retry_at_utc, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)"
                " ON CONFLICT (activity_id, kind) DO UPDATE SET"
                " current_payload_id = excluded.current_payload_id,"
                " json_pointer = excluded.json_pointer, state = excluded.state,"
                " last_attempt_at_utc = excluded.last_attempt_at_utc,"
                " last_success_at_utc = excluded.last_success_at_utc,"
                " next_retry_at_utc = NULL, error_code = NULL",
                (
                    activity_id,
                    ACTIVITY_RESOURCE_KIND_SEARCH_ITEM,
                    search_page_payload_id,
                    json_pointer,
                    STATE_AVAILABLE,
                    now,
                    now,
                ),
            )

        self._run(work)


@dataclass(frozen=True)
class RawPayload:
    """One ``raw_payloads`` row; ``body_blob`` holds the authoritative bytes."""

    id: int
    resource_key: str
    activity_id: str | None
    kind: str
    content_type: str | None
    encoding: str | None
    body_text: str | None
    body_blob: bytes
    sha256: str
    byte_count: int
    first_seen_at_utc: str


@dataclass(frozen=True)
class FetchEvent:
    """One ``fetch_events`` row; ``request_params`` is the parsed JSON object or ``None``."""

    id: int
    sync_run_id: int | None
    resource_key: str
    method: str
    path: str
    request_params: dict[str, object] | None
    fetched_at_utc: str
    http_status: int | None
    payload_id: int | None
    etag: str | None
    last_modified: str | None
    error_code: str | None


_PAYLOAD_COLUMNS = (
    "id, resource_key, activity_id, kind, content_type, encoding, body_text, body_blob,"
    " sha256, byte_count, first_seen_at_utc"
)
_FETCH_EVENT_COLUMNS = (
    "id, sync_run_id, resource_key, method, path, request_params_json, fetched_at_utc,"
    " http_status, payload_id, etag, last_modified, error_code"
)


def _payload_from_row(row: tuple[object, ...]) -> RawPayload:
    (pid, key, activity_id, kind, ctype, enc, text, blob, sha, count, seen) = row
    return RawPayload(
        id=int(str(pid)),
        resource_key=str(key),
        activity_id=None if activity_id is None else str(activity_id),
        kind=str(kind),
        content_type=None if ctype is None else str(ctype),
        encoding=None if enc is None else str(enc),
        body_text=None if text is None else str(text),
        body_blob=blob if isinstance(blob, bytes) else b"",
        sha256=str(sha),
        byte_count=int(str(count)),
        first_seen_at_utc=str(seen),
    )


def _fetch_event_from_row(row: tuple[object, ...]) -> FetchEvent:
    (eid, run_id, key, method, path, params_json, fetched, status, payload_id, etag, lm, err) = row
    params: dict[str, object] | None = None
    if params_json is not None:
        parsed = cast(object, json.loads(str(params_json)))
        if isinstance(parsed, dict):
            params = cast(dict[str, object], parsed)
    return FetchEvent(
        id=int(str(eid)),
        sync_run_id=None if run_id is None else int(str(run_id)),
        resource_key=str(key),
        method=str(method),
        path=str(path),
        request_params=params,
        fetched_at_utc=str(fetched),
        http_status=None if status is None else int(str(status)),
        payload_id=None if payload_id is None else int(str(payload_id)),
        etag=None if etag is None else str(etag),
        last_modified=None if lm is None else str(lm),
        error_code=None if err is None else str(err),
    )


def canonical_json_hash(value: object) -> str:
    """SHA-256 of ``value`` as sorted-key, compact JSON; for comparison only."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArchiveReader:
    """Read-only access to archived payloads and fetch history.

    Performs no writes, so it works on read-only and read-write connections.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        try:
            return [tuple(r) for r in self._conn.execute(sql, params).fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Archive read failed: {exc}") from exc

    def get_payload(self, payload_id: int) -> RawPayload:
        """Return the payload with ``payload_id``; raise ``KeyError`` if absent."""
        rows = self._query(
            f"SELECT {_PAYLOAD_COLUMNS} FROM raw_payloads WHERE id = ?", (payload_id,)
        )
        if not rows:
            raise KeyError(payload_id)
        return _payload_from_row(rows[0])

    def current_payload(self, activity_id: str, kind: str) -> RawPayload | None:
        """Return the payload an activity resource currently points at, or ``None``."""
        rows = self._query(
            "SELECT current_payload_id FROM activity_resources WHERE activity_id = ? AND kind = ?",
            (activity_id, kind),
        )
        if not rows or rows[0][0] is None:
            return None
        return self.get_payload(int(str(rows[0][0])))

    def payload_versions(self, resource_key: str) -> list[RawPayload]:
        """Return every stored version of ``resource_key`` in id order."""
        rows = self._query(
            f"SELECT {_PAYLOAD_COLUMNS} FROM raw_payloads WHERE resource_key = ? ORDER BY id",
            (resource_key,),
        )
        return [_payload_from_row(r) for r in rows]

    def resource_states(self, activity_id: str) -> dict[str, str]:
        """Return ``{kind: state}`` for every resource row of ``activity_id``."""
        rows = self._query(
            "SELECT kind, state FROM activity_resources WHERE activity_id = ? ORDER BY kind",
            (activity_id,),
        )
        return {str(k): str(s) for k, s in rows}

    def pending_resources(self) -> list[tuple[str, str]]:
        """Return ``(activity_id, kind)`` of every ``pending`` resource, ordered by both."""
        rows = self._query(
            "SELECT activity_id, kind FROM activity_resources WHERE state = ?"
            " ORDER BY activity_id, kind",
            (STATE_PENDING,),
        )
        return [(str(a), str(k)) for a, k in rows]

    def coverage(self) -> dict[str, dict[str, int]]:
        """Return ``{kind: {state: count}}`` for summary, workouts and original_file.

        Every kind and every known state is present, with ``0`` for absent combinations.
        """
        kinds = (KIND_SUMMARY, KIND_WORKOUTS, KIND_ORIGINAL_FILE)
        result = {kind: dict.fromkeys(RESOURCE_STATES, 0) for kind in kinds}
        rows = self._query(
            "SELECT kind, state, COUNT(*) FROM activity_resources"
            " WHERE kind IN (?, ?, ?) GROUP BY kind, state",
            kinds,
        )
        for kind, state, count in rows:
            result[str(kind)][str(state)] = int(str(count))
        return result

    def prior_search_item(self, activity_id: str) -> object | None:
        """Return the parsed search item an activity's ``search_item`` resource points at.

        Returns ``None`` if the activity has no search item or the pointer cannot be resolved.
        """
        rows = self._query(
            "SELECT current_payload_id, json_pointer FROM activity_resources"
            " WHERE activity_id = ? AND kind = ?",
            (activity_id, ACTIVITY_RESOURCE_KIND_SEARCH_ITEM),
        )
        if not rows or rows[0][0] is None or rows[0][1] is None:
            return None
        pointer = str(rows[0][1])
        if not _SEARCH_ITEM_POINTER.fullmatch(pointer):
            return None
        payload = self.get_payload(int(str(rows[0][0])))
        try:
            page = cast(object, json.loads(payload.body_blob))
        except ValueError:
            return None
        index = int(pointer[1:])
        if not isinstance(page, list):
            return None
        items = cast(list[object], page)
        return items[index] if index < len(items) else None

    def prior_search_item_hash(self, activity_id: str) -> str | None:
        """Return the canonical-JSON SHA-256 of the prior search item, or ``None``.

        The hash is for change detection only; nothing canonicalised is stored.
        """
        item = self.prior_search_item(activity_id)
        if item is None:
            return None
        return canonical_json_hash(item)

    def fetch_events(self, resource_key: str) -> list[FetchEvent]:
        """Return fetch events for ``resource_key`` in id order."""
        rows = self._query(
            f"SELECT {_FETCH_EVENT_COLUMNS} FROM fetch_events WHERE resource_key = ? ORDER BY id",
            (resource_key,),
        )
        return [_fetch_event_from_row(r) for r in rows]
