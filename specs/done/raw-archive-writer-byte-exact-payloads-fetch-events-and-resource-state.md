---
name: "Raw archive writer: byte-exact payloads, fetch events and resource state"
id: spec-8e57f10a
description: "Atomic byte-exact raw payload storage with dedupe/versioning, fetch events, resource state and read helpers (SPEC §5.1, §6.2)"
dependencies: null
priority: high
complexity: null
status: done
tags:
- storage
- archive
scope:
  in: null
  out: null
feature_root_id: B-528c9b69
---
# Raw archive writer: byte-exact payloads, fetch events and resource state

## Objective

Implement `src/garmin_archive/archive.py`, the transactional raw-archive write and read primitives that later sync code calls for every Garmin API response. It stores response bodies exactly as received (`docs/SPEC.md` §5.1), records each fetch observation, and keeps per-activity resource state, all committed atomically (SPEC §6.2).

Depends on the spec "Storage foundation: SQLite connections, migrations and archive schema" (schema `0001_archive.sql`, `transaction()`, `utc_rfc3339`, `Clock`, `StorageError`).

## Problems to Fix

1. SPEC §5.1 requires byte-exact raw storage, with a TEXT copy for decodable text and a BLOB as the authoritative copy, never re-serialized. It also requires deduplication of identical bodies per resource, immutable versions for changed bodies, and that nothing is deleted when a resource disappears upstream. No code does this yet.
2. SPEC §6.2 requires that payload, fetch event and resource pointer commit atomically, and that no write transaction is held during network I/O. Without one primitive, sync code would have to compose these writes itself and could get atomicity wrong.
3. SPEC §4 forbids persisting credentials. The fetch log stores request parameters, so a guard is needed.

## Changes

### 1. Resource keys

Define module constants and a `validate_resource_key(resource_key, kind, activity_id) -> None` that raises `ValueError` on any mismatch. Keys must match exactly one of these patterns, and the kind and activity ID embedded in the key must equal the `kind` and `activity_id` arguments:

| kind | Pattern | activity_id argument |
|---|---|---|
| `search_page` | `^search_page(:[a-zA-Z]+=[A-Za-z0-9_.-]+)+$`. Must include `start=` and `limit=`. Parameter names are sorted ascending. | must be `None` |
| `summary` | `^summary:(\d+)$` | must equal the captured group |
| `workouts` | `^workouts:(\d+)$` | must equal the group |
| `original_file` | `^original_file:(\d+)$` | must equal the group |

Helper constructors build valid keys:
- `search_page_key(params: Mapping[str, str | int | bool])`: booleans become `true`/`false`, keys are sorted.
- `summary_key(activity_id)`, `workouts_key(activity_id)`, `original_file_key(activity_id)`.

`imported_file` is a schema kind reserved for future local imports. SPEC v1 defines no import path. Every writer method rejects `kind='imported_file'` with `ValueError`.

### 2. `ArchiveWriter(conn: sqlite3.Connection, clock: Clock)`

The connection must be a write connection, `isolation_level=None`. Each public method runs in exactly **one** `transaction(conn)` and performs no network I/O. All timestamps come from `utc_rfc3339(clock.now())`.

Error rules for every method:
- Enum arguments (`kind`, `resource_state`, `availability_status`) and formats are validated in Python. Invalid values raise `ValueError` **before** any write.
- Any `sqlite3.Error` raised inside a method, such as an FK violation from an unknown `sync_run_id`, is re-raised as `StorageError` with `__cause__` set, after rollback.
- `sync_run_id: int | None`. `None` means the fetch happened outside a sync run. Tests that need a run insert a `sync_runs` row with raw SQL.
- `raw_payloads.activity_id` is `NULL` for `search_page` payloads and equals `activity_id` otherwise. `content_type` is stored exactly as received.

- **`upsert_activity(*, activity_id: str, owner_profile_id: str | None, parent_activity_id: str | None, type_key: str | None, availability_status: str = "listed") -> bool`**
  - Inserts a row with `first_seen_at_utc = last_seen_at_utc = now`.
  - If the row exists, overwrites `owner_profile_id`, `parent_activity_id`, `type_key` and `availability_status` **with the given values, including `None`**, sets `last_seen_at_utc = now`, and keeps `first_seen_at_utc`.
  - Returns `True` if inserted.
  - `activity_id` must be all digits; otherwise it raises `ValueError`.

- **`record_response(*, resource_key, kind, activity_id, sync_run_id, method, path, request_params, http_status, content_type, body: bytes, etag=None, last_modified=None, resource_state="available", json_pointer=None) -> RecordResult`**
  1. Validate the resource key.
  2. Validate `request_params` (§4).
  3. Compute `sha256` (lowercase hex) and `byte_count = len(body)`.
  4. Derive `body_text` and `encoding`:
     - Text media types are `application/json`, `*/*+json` and `text/*`. Parse the media type case-insensitively and ignore parameters other than `charset`.
     - For a text media type, decode with the `charset` parameter (default `utf-8`). On success `encoding` = the charset, lower-cased. On a decode error or unknown codec, `body_text = NULL` and `encoding = NULL`.
     - Any other or missing content type gives `body_text = NULL` and `encoding = NULL`.
     - Never parse and re-serialize JSON.
  5. Insert into `raw_payloads`. On `UNIQUE(resource_key, sha256)` conflict, reuse the existing row's `id` and leave the row completely unchanged, including `first_seen_at_utc`.
  6. Insert a `fetch_events` row with `fetched_at_utc = now`, the given request fields, `payload_id`, `etag`, `last_modified` and `error_code = NULL`.
  7. If `kind != "search_page"`, upsert `activity_resources(activity_id, kind)`:
     - `current_payload_id = payload_id`
     - `state = resource_state`, which must be one of the schema states
     - `json_pointer = json_pointer`
     - `last_attempt_at_utc = last_success_at_utc = now`
     - `next_retry_at_utc = NULL`, `error_code = NULL`

     The `activity_archive` row must already exist. If it doesn't, raise `StorageError` and roll back everything.
  8. Return `RecordResult(payload_id: int, fetch_event_id: int, is_new_payload: bool, sha256: str)`.

- **`record_failure(*, resource_key, kind, activity_id, sync_run_id, method, path, request_params, http_status: int | None, error_code: str, resource_state: str, next_retry_at_utc: str | None = None) -> int`**
  - `resource_state` is one of `unavailable`, `forbidden` or `failed`.
  - Validates the key and params.
  - Inserts a `fetch_events` row with `payload_id = NULL`.
  - If `kind != "search_page"`, upserts the resource row with `state`, `error_code`, `last_attempt_at_utc = now` and `next_retry_at_utc`. It **keeps** `current_payload_id`, `last_success_at_utc` and `json_pointer` when the row exists. On insert, these are NULL.
  - Requires an existing `activity_archive` row for non-search kinds.
  - Error bodies are never stored.
  - Returns the fetch event id.

- **`record_search_item(*, activity_id: str, search_page_payload_id: int, json_pointer: str) -> None`**
  - Requires that the `activity_archive` row exists and that the payload exists with `kind = 'search_page'`. Otherwise it raises `StorageError`.
  - `json_pointer` must match `^/\d+$`; otherwise it raises `ValueError`.
  - Upserts `activity_resources(activity_id, 'search_item')` with `current_payload_id`, `json_pointer`, `state = 'available'`, `last_attempt_at_utc = last_success_at_utc = now`, and `error_code` and `next_retry_at_utc` set to NULL.

- **`mark_summary_check(*, activity_id: str, availability_status: str) -> None`**
  - Sets `activity_archive.last_summary_check_at_utc = now` and `availability_status`, which is one of `listed`, `not_found` or `forbidden`.
  - Does **not** touch `last_seen_at_utc` or any other column.
  - A missing activity row raises `StorageError`.
  - Sync uses it after a summary fetch, including a 404 → `not_found` (SPEC §5.2). It never deletes anything.

### 3. `ArchiveReader(conn)`

Works on read or write connections and performs no writes.

- `RawPayload` is a frozen dataclass with every `raw_payloads` column. `body_blob` is `bytes`.
- `get_payload(payload_id) -> RawPayload` raises `KeyError` if absent.
- `current_payload(activity_id, kind) -> RawPayload | None` follows `activity_resources.current_payload_id`.
- `payload_versions(resource_key) -> list[RawPayload]` is ordered by `id` ascending.
- `resource_states(activity_id) -> dict[str, str]` maps kind to state.
- `fetch_events(resource_key) -> list[FetchEvent]` is ordered by `id`. `FetchEvent` is a frozen dataclass of the columns, with `request_params` parsed into a dict.

### 4. Small fix carried over from the foundation spec

`output.error(...)` currently emits `"details": null` when no details are given. It must emit `"details": {}` (SPEC §9 error shape). Update `src/garmin_archive/output.py` and its test.

### 5. Request-parameter guard

`request_params` is `Mapping[str, str | int | bool] | None`. It is serialized as `json.dumps(params, sort_keys=True, separators=(",", ":"))`. A key matching `(?i)(auth|token|key|secret|password|cookie)` raises `ValueError` before any write. Values are never inspected for secrets; callers must not pass credentials, and the guard is defence in depth.

## Files to Modify

| File | Change |
|---|---|
| `src/garmin_archive/archive.py` | New: keys, `ArchiveWriter`, `ArchiveReader`, dataclasses |
| `src/garmin_archive/__init__.py` | Re-export `ArchiveWriter`, `ArchiveReader`, `RawPayload`, `RecordResult` and `FetchEvent` |
| `tests/test_archive.py` (and helpers) | New |
| `src/garmin_archive/output.py`, `tests/test_output.py` | `details` defaults to `{}` |

Do not change the schema migration from the foundation spec. If a schema change is genuinely required, stop and flag it.

## Acceptance Criteria

All tests use a database created with `ArchiveClient.initialize()` in `tmp_path` and a fixed clock.

1. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` pass with zero errors.
2. Resource keys:
   - For every kind, one valid key passes, and an invalid key raises `ValueError`. The invalid case per kind is: a wrong prefix, a non-digit ID, an `activity_id` that disagrees with the key, and a search page with unsorted params or missing `limit`.
   - The helper constructors' output passes validation.
3. Byte-exact storage with the committed sanitized fixture `tests/fixtures/contract/summaries/running-1.json`, read as bytes:
   - The stored `body_blob` equals the input bytes, and `sha256` equals `hashlib.sha256(bytes).hexdigest()`.
   - `body_text == bytes.decode("utf-8")` exactly, and `encoding == "utf-8"` for `application/json;charset=UTF-8`.
   - `SELECT json_extract(body_text, '$.summaryDTO.distance')` returns the fixture's value.
4. Binary and undecodable bodies:
   - An in-memory ZIP with content type `application/x-zip-compressed` is stored with `body_text IS NULL`, a byte-identical blob, and a correct SHA-256.
   - Invalid UTF-8 bytes with `application/json` give `body_text IS NULL`, `encoding IS NULL` and an intact blob.
   - `text/plain; charset=latin-1` decodes with `encoding == "latin-1"`.
5. Deduplication and versions:
   - Recording the same body twice for one resource key yields 1 `raw_payloads` row and 2 `fetch_events` rows. The second result has `is_new_payload == False`, and `first_seen_at_utc` keeps the first clock value even after the clock advances.
   - A changed body yields a second payload row. `payload_versions` returns both in order, the first one's bytes are unchanged, and `current_payload` returns the new one.
6. Atomicity:
   - `record_response` for an activity with no `activity_archive` row raises `StorageError`, and the counts of `raw_payloads`, `fetch_events` and `activity_resources` are unchanged.
   - Failure injection: create a `TEMP` trigger on the test connection (`CREATE TEMP TRIGGER inj BEFORE INSERT ON activity_resources BEGIN SELECT RAISE(ABORT, 'injected'); END;`). Then `record_response` raises `StorageError`, and the counts of `raw_payloads`, `fetch_events` and `activity_resources` are unchanged. The same check with a trigger on `fetch_events` also leaves `raw_payloads` unchanged.
7. `record_failure` after a success:
   - `state` and `error_code` are updated, and `current_payload_id` and `last_success_at_utc` are unchanged.
   - A fetch event with `payload_id IS NULL` and the error code is inserted.
   - A `next_retry_at_utc` value round-trips.
8. `record_search_item`:
   - It stores a `search_item` row pointing at a `search_page` payload with pointer `/3`.
   - A non-`search_page` payload, a missing activity, or the pointer `3` raise.
9. `upsert_activity`:
   - The insert returns `True`.
   - The update returns `False`, overwrites `type_key` (including to `None`), keeps `first_seen_at_utc`, and advances `last_seen_at_utc`.
   - A non-digit ID raises `ValueError`.
10. Request params with keys `Authorization`, `access_token` or `apiKey` raise `ValueError`, and no rows are written. Allowed params are stored as compact, sorted JSON.
11. All `*_at_utc` values written equal the fixed clock's RFC 3339 `Z` strings.
12. No method of `ArchiveWriter` leaves `conn.in_transaction` true after it returns or raises.
13. An invalid `resource_state` or `availability_status`, or `kind='imported_file'`, raises `ValueError` and writes nothing. An unknown `sync_run_id` raises `StorageError` and writes nothing.
14. `mark_summary_check(..., availability_status='not_found')` sets the status and `last_summary_check_at_utc`, leaves `last_seen_at_utc` unchanged, and keeps all payloads and resources.
15. `output.error("X", "m")` produces `details == {}`.

## Pending Decisions

- **Resolved:** The module lives at `src/garmin_archive/archive.py`, as SPEC §3 places it.
- **Resolved:** `upsert_activity` overwrites mutable fields with the values given. The caller (sync) always passes the latest list-item values.
- **Resolved:** `imported_file` is reserved and rejected in v1. SPEC v1 has no import path.
- **Out of scope:** HTTP calls, sync orchestration, account binding calls (which belong to sync), export and validation.
