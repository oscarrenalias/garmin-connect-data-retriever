from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from garmin_archive import (
    ArchiveClient,
    ArchiveConfig,
    ArchiveReader,
    ArchiveWriter,
    FetchEvent,
    RawPayload,
    RecordResult,
)
from garmin_archive.archive import (
    original_file_key,
    search_page_key,
    summary_key,
    validate_resource_key,
    workouts_key,
)
from garmin_archive.models import StorageError
from garmin_archive.output import error
from garmin_archive.storage.connection import open_read_connection, open_write_connection

FIXTURE = Path(__file__).parent / "fixtures" / "contract" / "summaries" / "running-1.json"
T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
T0_S = "2026-01-02T03:04:05.000000Z"
T1 = T0 + timedelta(hours=1)
T1_S = "2026-01-02T04:04:05.000000Z"
AID = "12345"
PAGE_KEY = "search_page:limit=20:start=0"


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "a.db"
    ArchiveClient(ArchiveConfig(db_path=path), clock=FakeClock(), environ={}).initialize()
    return path


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_write_connection(db_path, busy_timeout_ms=1000)
    yield c
    c.close()


@pytest.fixture
def writer(conn: sqlite3.Connection, clock: FakeClock) -> ArchiveWriter:
    return ArchiveWriter(conn, clock)


@pytest.fixture
def reader(conn: sqlite3.Connection) -> ArchiveReader:
    return ArchiveReader(conn)


def _activity(writer: ArchiveWriter, activity_id: str = AID) -> None:
    writer.upsert_activity(
        activity_id=activity_id, owner_profile_id="p1", parent_activity_id=None, type_key="running"
    )


def _summary(
    writer: ArchiveWriter,
    body: bytes,
    content_type: str | None = "application/json;charset=UTF-8",
    *,
    activity_id: str = AID,
    **kwargs: object,
) -> RecordResult:
    return writer.record_response(
        resource_key=summary_key(activity_id),
        kind="summary",
        activity_id=activity_id,
        sync_run_id=kwargs.pop("sync_run_id", None),  # type: ignore[arg-type]
        method="GET",
        path=f"/activities/{activity_id}",
        request_params=None,
        http_status=200,
        content_type=content_type,
        body=body,
        **kwargs,  # type: ignore[arg-type]
    )


def _page(writer: ArchiveWriter, body: bytes = b'[{"a":1}]') -> RecordResult:
    return writer.record_response(
        resource_key=PAGE_KEY,
        kind="search_page",
        activity_id=None,
        sync_run_id=None,
        method="GET",
        path="/activities",
        request_params={"start": 0, "limit": 20},
        http_status=200,
        content_type="application/json",
        body=body,
    )


def _failure(writer: ArchiveWriter, **overrides: object) -> int:
    args: dict[str, object] = {
        "resource_key": summary_key(AID),
        "kind": "summary",
        "activity_id": AID,
        "sync_run_id": None,
        "method": "GET",
        "path": f"/activities/{AID}",
        "request_params": None,
        "http_status": 404,
        "error_code": "NOT_FOUND",
        "resource_state": "unavailable",
    }
    args.update(overrides)
    return writer.record_failure(**args)  # type: ignore[arg-type]


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("raw_payloads", "fetch_events", "activity_resources", "activity_archive")
    )


# --- AC2: resource keys ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "kind", "aid"),
    [
        ("search_page:limit=20:start=0", "search_page", None),
        ("search_page:limit=20:order=a-b_c.d:start=0", "search_page", None),
        ("summary:123", "summary", "123"),
        ("workouts:123", "workouts", "123"),
        ("original_file:123", "original_file", "123"),
    ],
)
def test_valid_keys_pass(key: str, kind: str, aid: str | None) -> None:
    validate_resource_key(key, kind, aid)


@pytest.mark.parametrize(
    ("key", "kind", "aid"),
    [
        ("summary:123", "workouts", "123"),  # wrong prefix for kind
        ("workouts:123", "summary", "123"),
        ("original_file:123", "summary", "123"),
        ("search_page:limit=20:start=0", "summary", None),
        ("summary:12a", "summary", "12a"),  # non-digit id
        ("workouts:abc", "workouts", "abc"),
        ("original_file:", "original_file", ""),
        ("summary:123", "summary", "456"),  # disagreeing activity_id
        ("summary:123", "summary", None),
        ("workouts:123", "workouts", "1234"),
        ("original_file:123", "original_file", "124"),
        ("summary:123\n", "summary", "123"),  # trailing newline (fullmatch)
        ("search_page:limit=20:start=0\n", "search_page", None),
        ("search_page:start=0:limit=20", "search_page", None),  # unsorted
        ("search_page:start=0", "search_page", None),  # missing limit
        ("search_page:limit=20", "search_page", None),  # missing start
        ("search_page", "search_page", None),
        ("search_page:limit=20:start=0", "search_page", "123"),  # activity id given
        ("search_page:limit=20:start=a b", "search_page", None),  # disallowed char
        ("imported_file:123", "imported_file", "123"),
        ("summary:123", "bogus", "123"),
    ],
)
def test_invalid_keys_raise(key: str, kind: str, aid: str | None) -> None:
    with pytest.raises(ValueError):
        validate_resource_key(key, kind, aid)


def test_key_constructors_produce_valid_keys() -> None:
    validate_resource_key(summary_key("9"), "summary", "9")
    validate_resource_key(workouts_key("9"), "workouts", "9")
    validate_resource_key(original_file_key("9"), "original_file", "9")
    key = search_page_key({"start": 0, "limit": 20, "includeAll": True, "x": False})
    assert key == "search_page:includeAll=true:limit=20:start=0:x=false"
    validate_resource_key(key, "search_page", None)


def test_search_page_key_requires_start_and_limit() -> None:
    with pytest.raises(ValueError):
        search_page_key({"start": 0})
    with pytest.raises(ValueError):
        search_page_key({"limit": 5, "bad": "a b"})


# --- AC3: byte-exact storage ----------------------------------------------------------


def test_byte_exact_fixture_storage(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection
) -> None:
    data = FIXTURE.read_bytes()
    _activity(writer)
    result = _summary(writer, data)
    assert result.is_new_payload is True
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    payload = reader.get_payload(result.payload_id)
    assert isinstance(payload, RawPayload)
    assert payload.body_blob == data
    assert payload.sha256 == hashlib.sha256(data).hexdigest()
    assert payload.byte_count == len(data)
    assert payload.body_text == data.decode("utf-8")
    assert payload.encoding == "utf-8"
    assert payload.content_type == "application/json;charset=UTF-8"
    assert payload.activity_id == AID
    assert payload.kind == "summary"
    got = conn.execute(
        "SELECT json_extract(body_text, '$.summaryDTO.distance') FROM raw_payloads WHERE id = ?",
        (result.payload_id,),
    ).fetchone()[0]
    assert got == json.loads(data)["summaryDTO"]["distance"]


def test_json_text_is_not_reserialized(writer: ArchiveWriter, reader: ArchiveReader) -> None:
    body = b'{ "b":1,   "a" : 2.50 }\n'
    _activity(writer)
    result = _summary(writer, body, "application/json")
    assert reader.get_payload(result.payload_id).body_text == '{ "b":1,   "a" : 2.50 }\n'


# --- AC4: binary / undecodable --------------------------------------------------------


def test_zip_body_has_no_text(writer: ArchiveWriter, reader: ArchiveReader) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.fit", b"\x00\x01\xff\xfe")
    data = buf.getvalue()
    _activity(writer)
    result = writer.record_response(
        resource_key=original_file_key(AID),
        kind="original_file",
        activity_id=AID,
        sync_run_id=None,
        method="GET",
        path=f"/activities/{AID}/file",
        request_params=None,
        http_status=200,
        content_type="application/x-zip-compressed",
        body=data,
    )
    payload = reader.get_payload(result.payload_id)
    assert payload.body_text is None
    assert payload.encoding is None
    assert payload.body_blob == data
    assert payload.sha256 == hashlib.sha256(data).hexdigest()


def test_invalid_utf8_json_has_no_text(writer: ArchiveWriter, reader: ArchiveReader) -> None:
    body = b'{"a":"\xff\xfe"}'
    _activity(writer)
    payload = reader.get_payload(_summary(writer, body, "application/json").payload_id)
    assert payload.body_text is None
    assert payload.encoding is None
    assert payload.body_blob == body


def test_latin1_text(writer: ArchiveWriter, reader: ArchiveReader) -> None:
    body = "caf\xe9".encode("latin-1")
    _activity(writer)
    payload = reader.get_payload(_summary(writer, body, "text/plain; charset=latin-1").payload_id)
    assert payload.encoding == "latin-1"
    assert payload.body_text == "caf\xe9"
    assert payload.body_blob == body


@pytest.mark.parametrize(
    ("content_type", "text", "encoding"),
    [
        ("Application/JSON", "{}", "utf-8"),
        ("application/vnd.foo+json", "{}", "utf-8"),
        ('application/json; charset="UTF-8"', "{}", "utf-8"),
        ("text/plain", "{}", "utf-8"),
        ("application/json;charset=bogus-codec", None, None),
        ("application/octet-stream", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_content_type_handling(
    writer: ArchiveWriter,
    reader: ArchiveReader,
    content_type: str | None,
    text: str | None,
    encoding: str | None,
) -> None:
    _activity(writer)
    payload = reader.get_payload(_summary(writer, b"{}", content_type).payload_id)
    assert payload.body_text == text
    assert payload.encoding == encoding
    assert payload.content_type == content_type
    assert payload.body_blob == b"{}"


# --- AC5: dedupe and versions ---------------------------------------------------------


def test_same_body_dedupes(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    data = FIXTURE.read_bytes()
    _activity(writer)
    first = _summary(writer, data)
    clock.set(T1)
    second = _summary(writer, data)
    assert first.is_new_payload is True
    assert second.is_new_payload is False
    assert second.payload_id == first.payload_id
    assert second.fetch_event_id != first.fetch_event_id
    assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fetch_events").fetchone()[0] == 2
    assert reader.get_payload(first.payload_id).first_seen_at_utc == T0_S
    events = reader.fetch_events(summary_key(AID))
    assert [e.fetched_at_utc for e in events] == [T0_S, T1_S]
    assert [e.payload_id for e in events] == [first.payload_id] * 2


def test_changed_body_creates_new_version(
    writer: ArchiveWriter, reader: ArchiveReader, clock: FakeClock
) -> None:
    _activity(writer)
    first = _summary(writer, b'{"v":1}')
    clock.set(T1)
    second = _summary(writer, b'{"v":2}')
    assert second.is_new_payload is True
    assert second.payload_id != first.payload_id
    versions = reader.payload_versions(summary_key(AID))
    assert [v.id for v in versions] == [first.payload_id, second.payload_id]
    assert versions[0].body_blob == b'{"v":1}'
    assert versions[1].body_blob == b'{"v":2}'
    assert versions[0].first_seen_at_utc == T0_S
    assert versions[1].first_seen_at_utc == T1_S
    current = reader.current_payload(AID, "summary")
    assert current is not None
    assert current.id == second.payload_id


def test_old_body_returning_repoints_resource(writer: ArchiveWriter, reader: ArchiveReader) -> None:
    _activity(writer)
    first = _summary(writer, b"A")
    _summary(writer, b"B")
    again = _summary(writer, b"A")
    assert again.is_new_payload is False
    current = reader.current_payload(AID, "summary")
    assert current is not None
    assert current.id == first.payload_id
    assert len(reader.payload_versions(summary_key(AID))) == 2


# --- record_response details ----------------------------------------------------------


def test_search_page_has_no_activity_and_no_resource_row(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    result = _page(writer)
    row = conn.execute(
        "SELECT activity_id, kind FROM raw_payloads WHERE id = ?", (result.payload_id,)
    ).fetchone()
    assert tuple(row) == (None, "search_page")
    assert conn.execute("SELECT COUNT(*) FROM activity_resources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM activity_archive").fetchone()[0] == 0


def test_record_response_resource_row_and_event_fields(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    conn.execute(
        "INSERT INTO sync_runs (id, mode, started_at_utc, status)"
        " VALUES (7, 'full', 't', 'running')"
    )
    result = _summary(
        writer,
        b"{}",
        sync_run_id=7,
        etag="e1",
        last_modified="lm",
        json_pointer="/0",
    )
    row = conn.execute(
        "SELECT current_payload_id, json_pointer, state, last_attempt_at_utc,"
        " last_success_at_utc, next_retry_at_utc, error_code FROM activity_resources"
        " WHERE activity_id = ? AND kind = 'summary'",
        (AID,),
    ).fetchone()
    assert tuple(row) == (result.payload_id, "/0", "available", T0_S, T0_S, None, None)
    (event,) = reader.fetch_events(summary_key(AID))
    assert isinstance(event, FetchEvent)
    assert event.sync_run_id == 7
    assert event.etag == "e1"
    assert event.last_modified == "lm"
    assert event.method == "GET"
    assert event.path == f"/activities/{AID}"
    assert event.http_status == 200
    assert event.error_code is None
    assert event.request_params is None


def test_record_response_custom_resource_state(
    writer: ArchiveWriter, reader: ArchiveReader
) -> None:
    _activity(writer)
    _summary(writer, b"{}", resource_state="pending")
    assert reader.resource_states(AID) == {"summary": "pending"}


# --- AC6: atomicity -------------------------------------------------------------------


def test_missing_activity_rolls_back(writer: ArchiveWriter, conn: sqlite3.Connection) -> None:
    before = _counts(conn)
    with pytest.raises(StorageError):
        _summary(writer, b"{}")
    assert _counts(conn) == before
    assert not conn.in_transaction


@pytest.mark.parametrize("table", ["activity_resources", "fetch_events"])
def test_injected_failure_rolls_back_everything(
    writer: ArchiveWriter, conn: sqlite3.Connection, table: str
) -> None:
    _activity(writer)
    before = _counts(conn)
    conn.execute(
        f"CREATE TEMP TRIGGER inj BEFORE INSERT ON {table}"
        " BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(StorageError) as excinfo:
        _summary(writer, b"{}")
    assert isinstance(excinfo.value.__cause__, sqlite3.Error)
    assert _counts(conn) == before
    assert not conn.in_transaction
    conn.execute("DROP TRIGGER inj")
    assert _summary(writer, b"{}").is_new_payload is True


# --- AC7: record_failure --------------------------------------------------------------


def test_failure_after_success_keeps_payload_state(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    _activity(writer)
    ok = _summary(writer, b"{}", json_pointer="/0")
    clock.set(T1)
    event_id = _failure(writer, next_retry_at_utc="2026-02-01T00:00:00.000000Z")
    row = conn.execute(
        "SELECT current_payload_id, last_success_at_utc, json_pointer, state, error_code,"
        " last_attempt_at_utc, next_retry_at_utc FROM activity_resources"
        " WHERE activity_id = ? AND kind = 'summary'",
        (AID,),
    ).fetchone()
    assert tuple(row) == (
        ok.payload_id,
        T0_S,
        "/0",
        "unavailable",
        "NOT_FOUND",
        T1_S,
        "2026-02-01T00:00:00.000000Z",
    )
    ev = reader.fetch_events(summary_key(AID))[-1]
    assert ev.id == event_id
    assert ev.payload_id is None
    assert ev.error_code == "NOT_FOUND"
    assert ev.http_status == 404
    assert ev.fetched_at_utc == T1_S
    assert not conn.in_transaction


def test_failure_on_fresh_resource_inserts_null_row(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    _failure(writer, resource_state="failed", http_status=None, error_code="TIMEOUT")
    row = conn.execute(
        "SELECT current_payload_id, last_success_at_utc, json_pointer, state, error_code,"
        " next_retry_at_utc FROM activity_resources"
    ).fetchone()
    assert tuple(row) == (None, None, None, "failed", "TIMEOUT", None)


@pytest.mark.parametrize("state", ["available", "pending", "bogus"])
def test_failure_invalid_state_raises(
    writer: ArchiveWriter, conn: sqlite3.Connection, state: str
) -> None:
    _activity(writer)
    before = _counts(conn)
    with pytest.raises(ValueError):
        _failure(writer, resource_state=state)
    assert _counts(conn) == before
    assert not conn.in_transaction


def test_failure_missing_activity_raises(writer: ArchiveWriter, conn: sqlite3.Connection) -> None:
    before = _counts(conn)
    with pytest.raises(StorageError):
        _failure(writer)
    assert _counts(conn) == before
    assert not conn.in_transaction


def test_search_page_failure_only_writes_fetch_event(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    _failure(
        writer,
        resource_key=PAGE_KEY,
        kind="search_page",
        activity_id=None,
        request_params={"start": 0, "limit": 20},
        http_status=500,
        error_code="SERVER",
        resource_state="failed",
    )
    assert _counts(conn) == (0, 1, 0, 0)


# --- AC8: record_search_item ----------------------------------------------------------


def test_record_search_item_happy_path(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    page = _page(writer)
    writer.record_search_item(
        activity_id=AID, search_page_payload_id=page.payload_id, json_pointer="/3"
    )
    row = conn.execute(
        "SELECT current_payload_id, json_pointer, state, last_attempt_at_utc,"
        " last_success_at_utc, error_code, next_retry_at_utc FROM activity_resources"
        " WHERE activity_id = ? AND kind = 'search_item'",
        (AID,),
    ).fetchone()
    assert tuple(row) == (page.payload_id, "/3", "available", T0_S, T0_S, None, None)
    assert reader.resource_states(AID) == {"search_item": "available"}
    assert not conn.in_transaction


@pytest.mark.parametrize("pointer", ["3", "/", "/a", "/-1", "/3/4", ""])
def test_record_search_item_bad_pointer(
    writer: ArchiveWriter, conn: sqlite3.Connection, pointer: str
) -> None:
    _activity(writer)
    page = _page(writer)
    before = _counts(conn)
    with pytest.raises(ValueError):
        writer.record_search_item(
            activity_id=AID, search_page_payload_id=page.payload_id, json_pointer=pointer
        )
    assert _counts(conn) == before


def test_record_search_item_missing_activity(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    page = _page(writer)
    before = _counts(conn)
    with pytest.raises(StorageError):
        writer.record_search_item(
            activity_id=AID, search_page_payload_id=page.payload_id, json_pointer="/0"
        )
    assert _counts(conn) == before
    assert not conn.in_transaction


def test_record_search_item_wrong_kind_or_missing_payload(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    summary = _summary(writer, b"{}")
    before = _counts(conn)
    for payload_id in (summary.payload_id, 9999):
        with pytest.raises(StorageError):
            writer.record_search_item(
                activity_id=AID, search_page_payload_id=payload_id, json_pointer="/0"
            )
    assert _counts(conn) == before


def test_record_search_item_upsert_replaces_and_clears_failure(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    _activity(writer)
    page1 = _page(writer, b"[1]")
    writer.record_search_item(
        activity_id=AID, search_page_payload_id=page1.payload_id, json_pointer="/0"
    )
    conn.execute(
        "UPDATE activity_resources SET state = 'failed', error_code = 'X',"
        " next_retry_at_utc = 'later' WHERE kind = 'search_item'"
    )
    clock.set(T1)
    page2 = _page(writer, b"[1,2]")
    writer.record_search_item(
        activity_id=AID, search_page_payload_id=page2.payload_id, json_pointer="/1"
    )
    row = conn.execute(
        "SELECT current_payload_id, json_pointer, state, error_code, next_retry_at_utc,"
        " last_success_at_utc FROM activity_resources WHERE kind = 'search_item'"
    ).fetchone()
    assert tuple(row) == (page2.payload_id, "/1", "available", None, None, T1_S)
    current = reader.current_payload(AID, "search_item")
    assert current is not None
    assert current.id == page2.payload_id


# --- AC9: upsert_activity -------------------------------------------------------------


def test_upsert_activity_insert_then_update(
    writer: ArchiveWriter, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    assert writer.upsert_activity(
        activity_id=AID, owner_profile_id="o", parent_activity_id="99", type_key="running"
    )
    sql = (
        "SELECT owner_profile_id, parent_activity_id, type_key, availability_status,"
        " first_seen_at_utc, last_seen_at_utc, last_summary_check_at_utc"
        " FROM activity_archive WHERE activity_id = ?"
    )
    assert tuple(conn.execute(sql, (AID,)).fetchone()) == (
        "o", "99", "running", "listed", T0_S, T0_S, None,
    )  # fmt: skip
    clock.set(T1)
    assert (
        writer.upsert_activity(
            activity_id=AID,
            owner_profile_id=None,
            parent_activity_id=None,
            type_key=None,
            availability_status="forbidden",
        )
        is False
    )
    assert tuple(conn.execute(sql, (AID,)).fetchone()) == (
        None, None, None, "forbidden", T0_S, T1_S, None,
    )  # fmt: skip
    assert not conn.in_transaction


@pytest.mark.parametrize("bad", ["", "12a", "-1", "1.5", "٣٤", "12\n", " 1"])
def test_upsert_activity_bad_id(writer: ArchiveWriter, conn: sqlite3.Connection, bad: str) -> None:
    with pytest.raises(ValueError):
        writer.upsert_activity(
            activity_id=bad, owner_profile_id=None, parent_activity_id=None, type_key=None
        )
    assert _counts(conn) == (0, 0, 0, 0)


# --- AC10: request params guard -------------------------------------------------------


@pytest.mark.parametrize("name", ["Authorization", "access_token", "apiKey"])
def test_credential_param_names_rejected(
    writer: ArchiveWriter, conn: sqlite3.Connection, name: str
) -> None:
    _activity(writer)
    before = _counts(conn)
    with pytest.raises(ValueError) as excinfo:
        _summary_with_params(writer, {name: "SECRET-VALUE-123"})
    assert "SECRET-VALUE-123" not in str(excinfo.value)
    assert _counts(conn) == before
    with pytest.raises(ValueError):
        _failure(writer, request_params={name: "v"})
    assert _counts(conn) == before


def _summary_with_params(
    writer: ArchiveWriter, params: dict[str, str | int | bool]
) -> RecordResult:
    return writer.record_response(
        resource_key=summary_key(AID),
        kind="summary",
        activity_id=AID,
        sync_run_id=None,
        method="GET",
        path="/x",
        request_params=params,
        http_status=200,
        content_type="application/json",
        body=b"{}",
    )


def test_allowed_params_stored_compact_sorted(
    writer: ArchiveWriter, conn: sqlite3.Connection, reader: ArchiveReader
) -> None:
    _activity(writer)
    _summary_with_params(writer, {"b": 1, "a": True, "c": "x"})
    raw = conn.execute("SELECT request_params_json FROM fetch_events").fetchone()[0]
    assert raw == '{"a":true,"b":1,"c":"x"}'
    assert reader.fetch_events(summary_key(AID))[0].request_params == {"a": True, "b": 1, "c": "x"}


# --- AC11: timestamps -----------------------------------------------------------------


def test_all_written_timestamps_equal_clock(
    writer: ArchiveWriter, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    clock.set(T1)
    _activity(writer)
    _summary(writer, b"{}")
    _failure(writer, resource_key=workouts_key(AID), kind="workouts", resource_state="failed")
    page = _page(writer)
    writer.record_search_item(
        activity_id=AID, search_page_payload_id=page.payload_id, json_pointer="/0"
    )
    writer.mark_summary_check(activity_id=AID, availability_status="listed")
    queries = {
        "activity_archive": ["first_seen_at_utc", "last_seen_at_utc", "last_summary_check_at_utc"],
        "raw_payloads": ["first_seen_at_utc"],
        "fetch_events": ["fetched_at_utc"],
        "activity_resources": ["last_attempt_at_utc"],
    }
    for table, cols in queries.items():
        for col in cols:
            values = {r[0] for r in conn.execute(f"SELECT {col} FROM {table}")}
            assert values == {T1_S}, (table, col, values)
    values = {r[0] for r in conn.execute("SELECT last_success_at_utc FROM activity_resources")}
    assert values == {T1_S, None}


# --- AC12: in_transaction -------------------------------------------------------------


def test_in_transaction_false_after_every_method(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    def check() -> None:
        assert not conn.in_transaction

    _activity(writer)
    check()
    ok = _summary(writer, b"{}")
    check()
    page = _page(writer)
    check()
    _failure(writer)
    check()
    writer.record_search_item(
        activity_id=AID, search_page_payload_id=page.payload_id, json_pointer="/0"
    )
    check()
    writer.mark_summary_check(activity_id=AID, availability_status="not_found")
    check()
    assert ok.payload_id
    # raising paths
    with pytest.raises(ValueError):
        writer.upsert_activity(
            activity_id="x", owner_profile_id=None, parent_activity_id=None, type_key=None
        )
    check()
    with pytest.raises(StorageError):
        writer.mark_summary_check(activity_id="404", availability_status="listed")
    check()
    with pytest.raises(StorageError):
        _summary(writer, b"{}", activity_id="404")
    check()
    with pytest.raises(ValueError):
        _summary(writer, b"{}", resource_state="nope")
    check()


# --- AC13: validation, no writes ------------------------------------------------------


def test_invalid_resource_state_availability_and_imported_file(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    before = _counts(conn)
    with pytest.raises(ValueError):
        _summary(writer, b"{}", resource_state="bogus")
    with pytest.raises(ValueError):
        writer.upsert_activity(
            activity_id="1",
            owner_profile_id=None,
            parent_activity_id=None,
            type_key=None,
            availability_status="gone",
        )
    with pytest.raises(ValueError):
        writer.mark_summary_check(activity_id=AID, availability_status="gone")
    with pytest.raises(ValueError):
        writer.record_response(
            resource_key=f"imported_file:{AID}",
            kind="imported_file",
            activity_id=AID,
            sync_run_id=None,
            method="GET",
            path="/x",
            request_params=None,
            http_status=200,
            content_type="application/json",
            body=b"{}",
        )
    with pytest.raises(ValueError):
        _failure(
            writer,
            resource_key=f"imported_file:{AID}",
            kind="imported_file",
            resource_state="failed",
        )
    assert _counts(conn) == before
    assert not conn.in_transaction


def test_invalid_input_leaves_status_unchanged(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    with pytest.raises(ValueError):
        writer.mark_summary_check(activity_id=AID, availability_status="gone")
    row = conn.execute(
        "SELECT availability_status, last_summary_check_at_utc FROM activity_archive"
    ).fetchone()
    assert tuple(row) == ("listed", None)


def test_unknown_sync_run_id_is_storage_error(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    _activity(writer)
    before = _counts(conn)
    with pytest.raises(StorageError) as excinfo:
        _summary(writer, b"{}", sync_run_id=424242)
    assert isinstance(excinfo.value.__cause__, sqlite3.Error)
    assert _counts(conn) == before
    with pytest.raises(StorageError):
        _failure(writer, sync_run_id=424242)
    assert _counts(conn) == before
    assert not conn.in_transaction


# --- AC14: mark_summary_check ---------------------------------------------------------


def test_mark_summary_check_not_found_keeps_everything(
    writer: ArchiveWriter, reader: ArchiveReader, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    _activity(writer)
    ok = _summary(writer, FIXTURE.read_bytes())
    clock.set(T1)
    writer.mark_summary_check(activity_id=AID, availability_status="not_found")
    row = conn.execute(
        "SELECT availability_status, last_summary_check_at_utc, last_seen_at_utc,"
        " first_seen_at_utc, owner_profile_id, type_key FROM activity_archive"
        " WHERE activity_id = ?",
        (AID,),
    ).fetchone()
    assert tuple(row) == ("not_found", T1_S, T0_S, T0_S, "p1", "running")
    current = reader.current_payload(AID, "summary")
    assert current is not None
    assert current.id == ok.payload_id
    assert reader.resource_states(AID) == {"summary": "available"}
    assert _counts(conn) == (1, 1, 1, 1)


def test_mark_summary_check_missing_activity(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    with pytest.raises(StorageError):
        writer.mark_summary_check(activity_id="1", availability_status="not_found")
    assert not conn.in_transaction


def test_sqlite_error_becomes_storage_error_with_cause(db_path: Path, clock: FakeClock) -> None:
    locker = open_write_connection(db_path, busy_timeout_ms=1000)
    locker.execute("BEGIN EXCLUSIVE")
    c = open_write_connection(db_path, busy_timeout_ms=1)
    try:
        with pytest.raises(StorageError) as excinfo:
            _activity(ArchiveWriter(c, clock))
        assert isinstance(excinfo.value.__cause__, sqlite3.Error)
        assert not c.in_transaction
    finally:
        locker.execute("ROLLBACK")
        locker.close()
        c.close()


# --- ArchiveReader --------------------------------------------------------------------


def test_reader_missing_and_empty(reader: ArchiveReader) -> None:
    with pytest.raises(KeyError):
        reader.get_payload(1)
    assert reader.current_payload(AID, "summary") is None
    assert reader.payload_versions("summary:1") == []
    assert reader.fetch_events("summary:1") == []
    assert reader.resource_states(AID) == {}


def test_reader_current_payload_none_when_pointer_null(
    writer: ArchiveWriter, reader: ArchiveReader
) -> None:
    _activity(writer)
    _failure(writer)
    assert reader.current_payload(AID, "summary") is None
    assert reader.resource_states(AID) == {"summary": "unavailable"}


def test_reader_fetch_events_ordered_with_parsed_params(
    writer: ArchiveWriter, reader: ArchiveReader
) -> None:
    _page(writer, b"[1]")
    _page(writer, b"[2]")
    events = reader.fetch_events(PAGE_KEY)
    assert [e.id for e in events] == sorted(e.id for e in events)
    assert len(events) == 2
    assert events[0].request_params == {"limit": 20, "start": 0}


def test_reader_works_read_only_and_writes_nothing(db_path: Path, writer: ArchiveWriter) -> None:
    _activity(writer)
    ok = _summary(writer, b'{"k":1}')
    ro = open_read_connection(db_path, busy_timeout_ms=1000)
    try:
        before = ro.total_changes
        r = ArchiveReader(ro)
        assert r.get_payload(ok.payload_id).body_blob == b'{"k":1}'
        assert r.resource_states(AID) == {"summary": "available"}
        assert len(r.fetch_events(summary_key(AID))) == 1
        assert len(r.payload_versions(summary_key(AID))) == 1
        current = r.current_payload(AID, "summary")
        assert current is not None
        assert ro.total_changes == before
    finally:
        ro.close()


def test_package_exports() -> None:
    import garmin_archive

    for name in ("ArchiveWriter", "ArchiveReader", "RawPayload", "RecordResult", "FetchEvent"):
        assert name in garmin_archive.__all__
        assert hasattr(garmin_archive, name)


# --- AC15 -----------------------------------------------------------------------------


def test_output_error_details_default_empty() -> None:
    assert error("X", "m")["error"]["details"] == {}
