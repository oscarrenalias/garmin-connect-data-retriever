---
name: "Sync discovery: inventory pagination and change detection"
id: spec-636b09ff
description: "Full/dated inventory pagination with adaptive page size, per-page atomic archiving, item fingerprints for new/changed/unchanged, checkpoints, resume and honest completeness"
dependencies: null
priority: high
complexity: null
status: draft
tags:
- sync
scope:
  in: null
  out: null
feature_root_id: null
---
# Sync discovery: inventory pagination and change detection

## Objective

Implement the discovery phase of synchronization in `src/garmin_archive/sync/discovery.py`. It enumerates every accessible activity through the search endpoint (`docs/SPEC.md` §6.1), including children and with no sport filter. Along the way it:
- archives every search page exactly as received
- upserts the activity inventory and links each activity to its search item
- classifies each activity as new, changed or unchanged using per-item canonical fingerprints (SPEC §6.2)
- adapts the page size when the server returns 5xx
- keeps durable checkpoints for resume
- reports inventory completeness honestly

The next spec, "Sync fetch queue and update command", consumes the result to fetch per-activity resources.

Depends on the following merged code: storage foundation (`transaction`, `open_write_connection`, metadata helpers, `bind_account`, `writer_lock`), the raw archive writer (`ArchiveWriter`, `ArchiveReader`, `search_page_key`), and the HTTP adapter (`GarminApi.search_page`, `ApiResponse`, errors).

## Problems to Fix

1. There is no inventory yet. SPEC §6.1 requires full enumeration until an empty page, and does not allow a short page to be treated as the end. Children must be included. Overlapping pages must be deduplicated, and repeated or no-progress pages detected. A hard page ceiling or repeated-page failure counts as incomplete, never as success.
2. The live contract run found that `limit=99` returns HTTP 500 for this account while 49 works. The threshold depends on the data (`tests/fixtures/contract/contract-report.md`). Discovery must shrink the page size automatically and remember the size that works.
3. SPEC §6.2 requires change detection per list item via canonical JSON fingerprints, not a hash of the whole page. Checkpoints may advance only after the associated discovery work is durable.
4. SPEC §4 requires that ingestion from a different Garmin profile is rejected.

## Changes

### 1. Item parsing (`sync/items.py`)

- `parse_search_page(body: bytes) -> list[dict[str, object]]`:
  - The body must be a JSON array of objects.
  - Anything else raises `ContractMismatchError` with `details={"reason": "search_page_not_array"}`.
- `SearchItem.from_json(obj, index)` is a frozen dataclass with these fields:

  | Field | Source |
  | --- | --- |
  | `activity_id: str` | `str(obj["activityId"])`. A missing value or a non-integer raises `ContractMismatchError`. |
  | `parent_activity_id: str \| None` | `parentId`, as str if not null |
  | `owner_profile_id: str \| None` | `ownerId`, as str if not null |
  | `type_key: str \| None` | `activityType.typeKey` |
  | `start_time_local: str \| None` | `startTimeLocal`, verbatim |
  | `is_parent: bool` | `isParent` or `parent` is true |
  | `index: int` | the item's position in the page |
  | `fingerprint: str` | sha256 hex of `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` |

  - Garmin IDs arrive as JSON integers. Python's `json` keeps them exact, and they are always converted to `str` for storage.

### 2. Discovery algorithm (`sync/discovery.py`)

```python
run_discovery(
    *, conn, api: GarminApi, writer: ArchiveWriter, reader: ArchiveReader, clock,
    sync_run_id: int, scope: DiscoveryScope, resume: bool = False,
    max_pages: int = 10_000, min_page_size: int = 10,
    cancel: threading.Event | None = None,
) -> DiscoveryResult
```

`DiscoveryScope` is either `full()` or `dated(start_date, end_date)`. Its `checkpoint_key` is `inventory` for full scope and `inventory:<start>:<end>` for dated scope. Dated scope maps to the adapter's `start_date`/`end_date` parameters, with Garmin's inclusive local-date semantics.

**Page size:**
- The initial page size is `archive_metadata['search_page_size']['effective']` if present, else 99.
- Requesting a page with `limit=L`:
  - Use `max_attempts=1` while `L > min_page_size`, and the adapter default when `L == min_page_size`.
  - On `TransportError` with `details["http_status"]` in 500–599 and `L > min_page_size`, set `L = max(min_page_size, L // 2)` and retry the **same** `start`. Record `{from, to, start}` in `page_size_reductions` and increment `server_error_pages`.
  - The size never increases within a run.
- When the run ends with a complete inventory, store `archive_metadata['search_page_size'] = {"effective": L, "updated_at_utc": now}` so the next run starts at the working size.
- Any other error stops discovery and propagates to the caller:
  - `AuthFailedError`, `AuthorizationError`, `RateLimitedError`, `ContractMismatchError`
  - `TransportError` at the minimum size

  Pages already committed stay committed.

**Pagination:**
1. Set `start = 0`. When `resume=True` and a checkpoint exists for the scope, set `start = max(0, checkpoint.next_offset - L)` so the boundary page is re-fetched.
2. Loop:
   - Check `cancel` before each request. If it is set, stop with `inventory_status = "interrupted"`.
   - Fetch the page and parse its items.
   - **Account check:** read `get_account_binding(conn)`. If a binding exists and any item has a non-null `owner_profile_id` that differs from it, raise `AccountMismatchError` **before** writing anything for the page. Discovery never creates the binding; the fetch phase binds from a verified summary (SPEC §4).
   - In **one** transaction per page, do the following. The writer methods each open their own transaction, so implement this as an internal `DiscoveryRecorder` that executes the same SQL as the writer inside a single `transaction(conn)`, or extend `ArchiveWriter` with a batch context. Either is acceptable, but every row for a page must commit or roll back together, checkpoint included.
     - Record the page payload: kind `search_page`, `resource_key = search_page_key({...})` covering all sent params, `http_status` 200.
     - For each item in page order:
       - Skip items whose `activity_id` was already seen in this run. Count them in `duplicate_items`.
       - Classify the item. **New:** no `activity_archive` row exists. **Changed:** the fingerprint differs from the item at the previous `search_item` resource's payload and pointer, recomputed from that stored page text. **Unchanged:** the fingerprints match.
       - Upsert the activity with `owner_profile_id`, `parent_activity_id` and `type_key` from the item.
       - Record the search item with `json_pointer = f"/{index}"`.
     - Upsert `sync_checkpoints(scope)`: `next_offset = start + len(items)`, `run_id`, `window_start`/`window_end` (dated scope only), `last_completed_at_utc` NULL.
   - **Empty page:** the inventory is complete. Stop.
   - **Repeated page:** the page's activity-ID sequence equals the previous page's sequence. Stop with `inventory_status = "incomplete"` and reason `repeated_page`.
   - **No progress:** every item on a non-empty page was a duplicate. Treat this the same as a repeated page. Increment `start` only by the number of items returned; a short page does **not** end pagination.
   - **Page ceiling:** `max_pages` reached. Stop with `inventory_status = "incomplete"` and reason `page_ceiling`.
3. When the inventory is complete, set the checkpoint's `last_completed_at_utc = now` and `next_offset = 0`.
   - `provisional = duplicate_items > 0`. The listing shifted during the run, so concurrent changes may have been missed. The fetch spec reports this as provisional coverage.
   - For **full** scope only, compute `not_listed`: activity IDs in `activity_archive` that were not seen in this run. Never delete or modify them.

**Result:**

```python
DiscoveryResult(
    inventory_status: Literal["complete", "incomplete", "interrupted"],
    reason: str | None,
    provisional: bool,
    pages: int,
    page_size_initial: int,
    page_size_effective: int,
    page_size_reductions: list[dict[str, int]],
    server_error_pages: int,
    discovered: int,            # distinct activities seen
    new: list[str], changed: list[str], unchanged: list[str],   # activity IDs
    duplicate_items: int,
    not_listed: list[str],      # full scope only, else []
)
```

### 3. Constraints

- No SQLite transaction is open during any `api` call.
- Discovery never fetches summaries, workouts or files.
- All timestamps come from the injected clock.
- No secrets reach logs, results or the database. The search-page `resource_key` contains only documented query params.

### 4. Clarifications (normative)

1. **Failure logging.** On any `TransportError`, `RateLimitedError` or `ContractMismatchError` from `search_page`, discovery calls `writer.record_failure(kind="search_page", resource_key=search_page_key(<params sent>), activity_id=None, resource_state="failed", error_code=<error code>, http_status=<status or None>, ...)`. That happens in its own transaction, outside any page transaction, and before it reduces the page size or propagates. This follows SPEC §5.2: error observations belong in `fetch_events`.
2. **A known activity with no `search_item` resource**, for example one first archived by a single-activity fetch, is classified `changed`.
3. **Repeated and no-progress pages** are committed like any other page (payload, fetch event, items, checkpoint). Discovery then stops. `start` always advances by the number of items returned, and a short page never ends pagination. `server_error_pages` counts page-size reductions.
4. **Locking.** The caller (`update`, next spec) holds `writer_lock`. `run_discovery` never acquires it.

AC additions:
- AC 3 also asserts exactly two failure `fetch_events` with `http_status = 500` and `payload_id IS NULL`.
- AC 9 asserts that the checkpoint's `next_offset` equals the total items committed, including the repeated page.
- New AC 16: an activity row with no `search_item` resource, which then appears in the inventory, is classified `changed`.

## Files to Modify

| File | Change |
|---|---|
| `src/garmin_archive/sync/__init__.py`, `sync/items.py`, `sync/discovery.py` | New |
| `src/garmin_archive/archive.py` | Only if needed for the per-page batch transaction (additive; existing behaviour and tests unchanged) |
| `tests/sync/test_items.py`, `tests/sync/test_discovery.py`, `tests/sync/fake_garmin.py` | New |

`tests/sync/fake_garmin.py` is a reusable fake Personal API built on `httpx.MockTransport`. The next spec reuses it. It serves:
- a token endpoint
- a configurable list of search items, built by copying the committed sanitized fixtures in `tests/fixtures/contract/search-items/` with synthetic IDs and dates
- a configurable `max_ok_limit`: any `limit` above it returns 500
- injectable mutations between pages: insert an item, reorder items, change an item
- the per-activity endpoints, which the next spec uses

## Acceptance Criteria

All tests run against an `ArchiveClient.initialize()`d DB in `tmp_path` with the fake API, a fixed clock, and recording sleep. They never touch the network.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check` and `pyright`, with zero errors.
2. Full enumeration:
   - Setup: 250 items, `max_ok_limit=1000`, page size 99.
   - The run fetches 3 pages plus an empty page: `discovered == 250`, all IDs listed as new, `inventory_status == "complete"`.
   - The DB holds 4 `search_page` payloads, 250 `activity_archive` rows, and 250 `search_item` resources with pointers `/0`..`/98` etc.
   - Every request carried `excludeChildren=false&sortBy=startLocal&sortOrder=asc`.
3. Server-capped pages:
   - Setup: `max_ok_limit=30`, starting at 99.
   - Page size reduces 99→49→24, and enumeration completes.
   - `page_size_reductions == [{"from": 99, "to": 49, "start": 0}, {"from": 49, "to": 24, "start": 0}]` and `server_error_pages == 2`.
   - `archive_metadata['search_page_size']['effective'] == 24`.
   - A second run starts with `limit=24` and has no reductions.
4. Short page:
   - Setup: the fake returns at most 40 items per request regardless of `limit`.
   - Pagination continues until the empty page and discovers all items.
5. Second unchanged run: a second full run yields `new == []`, `changed == []`, `unchanged` has all items, and **no new** `raw_payloads` rows (identical page bytes dedupe). It does add new `fetch_events` rows.
6. Changed item:
   - Setup: one item's `activityName` (sanitized value) is altered between runs.
   - That ID, and only that ID, is in `changed`.
   - The page containing it gets a new payload version, and the old version's bytes are intact.
7. Concurrent insertion:
   - Setup: an item is inserted at position 0 after the first page is served, shifting later pages by one.
   - The shifted item is counted once (`duplicate_items == 1`), and `provisional == True`.
   - All original items are discovered.
8. Children and unknown types:
   - Setup: a multisport parent (`isParent: true`) with two children (`parentId` set), plus an item with `typeKey: "made_up_sport"`.
   - All are archived with `parent_activity_id` and `type_key` stored as given. The parent ID is stored even when the parent is absent from the inventory (there is no FK).
9. Repeated page: the fake returns the same non-empty page for every `start`. The run stops with `inventory_status == "incomplete"` and reason `repeated_page`.
10. Page ceiling: `max_pages=2` with 250 items gives `incomplete`, reason `page_ceiling`. The checkpoint `next_offset` equals the items committed.
11. Resume:
    - Setup: after an interrupted run (`cancel` set after page 2), a `resume=True` run starts at `next_offset - L`.
    - It completes with each activity recorded exactly once in `activity_archive`.
12. Atomicity:
    - Setup: a TEMP trigger raises on the 50th `search_item` insert of page 1.
    - No rows from page 1 remain: no payload, no activities, no checkpoint.
    - Discovery raises `StorageError`.
13. Account mismatch:
    - Setup: with a binding of profile A, a page whose items have `ownerId` B.
    - `AccountMismatchError` is raised and nothing from that page is written.
14. Dated scope:
    - `dated(2026-06-01, 2026-06-30)` sends `startDate`/`endDate`, uses checkpoint key `inventory:2026-06-01:2026-06-30`, and gives `not_listed == []`.
    - A full run's `not_listed` contains an activity present in the DB but absent from the fake inventory, and that row still exists.
15. No transaction is held during HTTP: a fake transport asserts `conn.in_transaction is False` on every request.

## Pending Decisions

- **Resolved:** Discovery checks `ownerId` against an existing binding but never creates one. Binding happens from a verified summary, in the fetch phase (SPEC §4).
- **Resolved:** The learned effective page size is persisted in `archive_metadata` and never increased within a run. A future run starts at the stored size. Probing upward again is out of scope.
- **Resolved:** "Provisional" coverage is defined as duplicates observed during the pass, meaning the listing moved underneath us.
- **Out of scope:** fetching summaries, workouts and files; the `update` command; run records (`sync_runs` creation and finish are owned by the next spec, and tests insert a `sync_runs` row directly); processing.
