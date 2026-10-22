---
name: Sync fetch queue and update command
id: spec-1117159f
description: "Sync runs, durable pending-resource queue with full/incremental/scoped/activity/resume/retry-failed modes, account binding, coverage/counters and the update CLI command"
dependencies: null
priority: high
complexity: null
status: draft
tags:
- sync
- cli
scope:
  in: null
  out: null
feature_root_id: null
---
# Sync fetch queue and update command

## Objective

Complete synchronization (`docs/SPEC.md` §6) with five pieces:

1. **Sync runs:** create and finish run records.
2. **Durable fetch queue:** decide which per-activity resources to fetch (summary, workouts, original file), and fetch and archive them for **every** sport.
3. **Account binding:** bind the database to the account from the first verified summary.
4. **Coverage and counters:** report them honestly.
5. **Entry points:** `ArchiveClient.update(SyncOptions)` and the `garmin-archive update` command, with all selectors (`--full`, `--from/--to`, `--activity-id`, `--resume`, `--retry-failed`, `--archive-only`).

Analytical processing does not exist yet. Every run is effectively archive-only.

Depends on merged code from the storage foundation, the raw archive writer, credentials, the HTTP adapter and sync discovery (`run_discovery`, `DiscoveryScope`, `DiscoveryResult`, `tests/sync/fake_garmin.py`).

## Problems to Fix

1. Discovery alone only archives search pages. SPEC §6.1 requires summary, workouts and original-file retrieval for every discovered activity, with responses persisted before any processing. Empty workout arrays count as complete. Missing originals appear in coverage but do not block anything.
2. SPEC §6.2 defines the incremental rules:
   - fetch new and missing/failed resources
   - refresh changed activities and those in a recent window (default 14 days)
   - `--full` re-fetches everything
   - the scoped, single-activity, resume and retry-failed modes
   - bounded concurrency (1)
   - no write transaction held during network I/O
3. SPEC §4 requires binding the database to `userProfileId` from a verified summary and rejecting other profiles.
4. SPEC §6.2 and §9 require counters, separate summary and original-file coverage, partial status with resume information, exit codes 0/3/4/6/130, and Ctrl-C handling that leaves a resumable checkpoint.

## Changes

### 1. Writer additions (`archive.py`, additive)

- `ArchiveWriter.mark_pending(*, activity_id: str, kinds: Sequence[str]) -> None` runs in one transaction. For each kind in `summary`, `workouts` and `original_file`, it upserts the resource row with `state='pending'`. It keeps `current_payload_id`, `last_success_at_utc` and `json_pointer`, and clears `error_code` and `next_retry_at_utc`.
- A `pending` resource that still has a `current_payload_id` means a refresh is queued. The previous payload remains the current one until a new response is recorded.

### 2. Options and result (`sync/models.py`)

```python
@dataclass(frozen=True)
class SyncOptions:
    full: bool = False
    from_date: date | None = None
    to_date: date | None = None
    activity_id: str | None = None
    resume_run_id: int | None = None
    retry_failed: bool = False
    archive_only: bool = False
    recent_window_days: int = 14
```

**Validation** raises `InvalidArgumentError` (new: code `INVALID_ARGUMENT`, exit 2). These selector combinations are rejected:

- More than one of these is set: `full`, a date window, `activity_id`, `resume_run_id`, `retry_failed`.
- Only one of `from_date` / `to_date` is given. Both are required together, inclusive, with `from_date <= to_date`.
- `activity_id` contains non-digits.
- `recent_window_days < 0`.

**Mode mapping** (to `sync_runs.mode`):

| Options | Mode |
| --- | --- |
| `full` | `full` |
| none | `incremental` |
| dates | `scoped` |
| `activity_id` | `activity` |
| `resume_run_id` | `resume` |
| `retry_failed` | `retry_failed` |

A `full` or `incremental` run on a DB with no completed full inventory (`sync_checkpoints['inventory'].last_completed_at_utc` is NULL) still uses discovery over all history. A mode of `incremental` on an empty DB therefore performs full discovery but does **not** force re-fetching of existing resources (SPEC §6.2).

**`SyncResult`** (frozen dataclass; JSON-serializable via `asdict`):

- `run_id`, `mode`
- `status`: `complete` | `partial` | `failed` | `interrupted`
- `started_at_utc`, `finished_at_utc`
- `discovery`: the `DiscoveryResult` as a dict, or `None` when discovery was skipped
- `counters`: dict, see §5
- `coverage`: dict, see §5
- `failures`: a list of `{"activity_id", "kind", "error_code", "http_status"}`, capped at 100 entries with `failures_truncated: bool`
- `resume`: `{"run_id": int, "command": "garmin-archive update --resume <id>"}` when status is `partial` or `interrupted`, otherwise `None`
- `warnings`: a list of strings. It always includes `"analytical processing is not available in this version; archive only"`.

### 3. Run orchestration (`sync/runner.py`, `ArchiveClient.update`)

`ArchiveClient.update(options, *, cancel: threading.Event | None = None, progress: Callable[[ProgressEvent], None] | None = None) -> SyncResult`.

1. Validate the options.
2. Resolve the DB path. It must already be initialized: call `check_schema`, and raise `NotInitializedError` or `DatabaseNotFoundError` otherwise. **`update` never creates or migrates a database implicitly.** The CLI tells the user to run `init`.
3. Acquire `writer_lock` for the whole run. If it is held, raise `ArchiveLockedError`.
4. Open a write connection. Insert a `sync_runs` row with `status='running'`, `mode`, and `parameters_json` set to the options as JSON, with dates in ISO form.
   - For `resume`: the referenced run must exist, otherwise `InvalidArgumentError`. If its status is still `running` (a crashed process), set it to `interrupted`. Record `{"resumes_run_id": n}` in the parameters.
5. **Discovery**, depending on mode:

   | Mode | Discovery call |
   | --- | --- |
   | `full`, `incremental` | `run_discovery(scope=full())` |
   | `scoped` | `dated(from, to)` |
   | `resume` | the original run's scope, with `resume=True` |
   | `activity`, `retry_failed` | skipped |

   The discovery parameters are `sync_run_id`, `cancel` and the configured page sizes.
6. **Queue building** (SPEC §6.2). Call `mark_pending` for these per-activity kinds, each batch in one transaction:

   | Mode | Kinds marked pending |
   | --- | --- |
   | `full` | all three kinds for **every** activity seen in this discovery |
   | `incremental`, `scoped` | every kind of a **new** or **changed** activity; every kind whose state is `pending`, `failed` or absent; and every kind of activities whose `start_time_local` date is within `recent_window_days` of `clock.now()`'s UTC date (the start date is read from the stored search item) |
   | `activity` | all three kinds of that activity |
   | `resume` | nothing new; the existing `pending` rows plus anything discovery marks new or changed |
   | `retry_failed` | resources in state `failed` (not `unavailable` or `forbidden`), across all activities |

   - In `incremental` and `scoped` mode, `unavailable` and `forbidden` resources are **not** retried. `--full` retries everything.
   - In `activity` mode, an activity with no `activity_archive` row is first inserted via `upsert_activity` with nulls, and its summary fills the fields.
7. **Fetch loop.** Process resources in the `pending` state ordered by `(activity_id, kind)`, with summary before workouts before original_file within an activity. Scope differs by mode:
   - `full`, `incremental`, `scoped` and `activity` process only resources marked in this run.
   - `resume` processes all pending resources.

   For each resource:
   - Check `cancel` before each request. If it is set, stop and give status `interrupted`.
   - Call the adapter (`summary`, `workouts` or `original_file`) with no transaction open.
   - On **2xx**:
     - Record the response with `record_response`. `resource_key` comes from the helpers, `request_params=None`, `resource_state='available'`, `sync_run_id` set.
     - For **summary**, parse the JSON after archiving:
       - If it is not an object, or `str(activityId)` differs from the requested ID, set the resource to `failed` with `error_code='CONTRACT_MISMATCH'` and continue.
       - Read `userProfileId`. Call `bind_account` (the first bind returns True) or verify it. An `AccountMismatchError` stops the whole run with status `failed`, exit 3. The mismatching payload stays archived: raw responses are never deleted.
       - Call `upsert_activity` with `owner_profile_id=userProfileId`, `parent_activity_id=parentId`, and `type_key=activityTypeDTO.typeKey` (falling back to the stored type key when absent).
       - Call `mark_summary_check(availability_status='listed')`.
     - For **workouts**, a JSON array (including `[]`) is complete. A non-array body is still archived, but the resource is marked `failed` with `CONTRACT_MISMATCH`.
     - For **original_file**, the body is archived as-is, whatever the content type. No ZIP validation happens in sync.
   - On **404**, call `record_failure` with `http_status=404`. For summary, use `state='unavailable'` and `error_code='NOT_FOUND'`, then `mark_summary_check(availability_status='not_found')`. For workouts and original_file, use `state='unavailable'` and `error_code='NOT_FOUND'`. Nothing is deleted.
   - On **`AuthorizationError` (403)**, call `record_failure` with `state='forbidden'` and `error_code='FORBIDDEN'`, then continue.
   - On **`TransportError`**, call `record_failure` with `state='failed'`, `error_code='TRANSPORT_ERROR'` and `next_retry_at_utc = now + 1h`, then continue.
   - On **`RateLimitedError`**, call `record_failure` (`failed`, `RATE_LIMITED`, `next_retry_at_utc = now + retry_after_s`), then **stop the fetch loop**. The status becomes `partial` and the rest stay `pending`. This is resumable.
   - On **`ContractMismatchError`**, record the failure as `failed` with `CONTRACT_MISMATCH`, then continue.
   - On **`AuthFailedError`**, stop the run with status `failed` and re-raise after finalizing.
   - Errors raised by discovery are handled the same way. `AuthFailedError` and `AccountMismatchError` give `failed`. `RateLimitedError` and `TransportError` at the minimum page size give `partial`. In that case the fetch loop still runs for the activities already discovered, unless the error was auth or account.
8. **Finalize** in one transaction:
   - `finished_at_utc`, and `counters_json` / `coverage_json` with the dicts from §5.
   - The status:
     - `complete`: discovery was complete (or skipped) **and** no resource of this run ended `failed` or `pending`.
     - `interrupted`: cancelled.
     - `failed`: auth or account error.
     - `partial`: anything else.
     - `unavailable` and `forbidden` resources do **not** prevent `complete`, because they are source-confirmed (SPEC §9). They appear in coverage warnings.
   - If mode is `full` and status is `complete`, set `archive_metadata['last_full_refresh_at_utc']`.
   - In every case, set `archive_metadata['last_sync'] = {"run_id", "mode", "status", "finished_at_utc"}`.
   - A `KeyboardInterrupt` raised inside the library is caught by `update`. It finalizes the run as `interrupted` (best effort) and then re-raises.
9. **Progress.** `progress` receives `ProgressEvent(phase: "discovery"|"fetch", done: int, total: int | None, activity_id: str | None)` after each page or resource. Library code never prints.

### 4. Concurrency and transactions

- HTTP concurrency is 1, and resources are fetched sequentially.
- No SQLite transaction is ever open during an adapter call.
- Every writer call is its own transaction.
- The DB connection is closed and the lock released in a `finally` block.

### 5. Counters and coverage

**`counters`** describe this run only:

| Counter | Meaning |
| --- | --- |
| `activities_discovered`, `activities_new`, `activities_changed`, `activities_unchanged` | From discovery. 0 when discovery is skipped. |
| `resources_queued` | Number of resources marked pending. |
| `resources_downloaded` | 2xx responses that created a **new** payload version. |
| `resources_unchanged` | 2xx responses deduped to the existing payload. |
| `resources_unavailable`, `resources_forbidden`, `resources_failed` | Per outcome. |
| `resources_remaining_pending` | Pending after the loop. |
| `activities_processed` | Always 0. Processing does not exist yet. |

**`coverage`** describes the whole archive after this run, computed with SQL over `activity_resources` and `activity_archive`:

- `activities_total`
- `inventory_status`: from this run's discovery, or the last completed one. Possible values are `complete`, `incomplete`, `interrupted`, `provisional` (complete but with duplicates seen) and `unknown`.
- `last_full_refresh_at_utc`
- `summary`, `workouts` and `original_file`: each `{available, unavailable, forbidden, failed, pending, missing}`, where `missing` means activities with no row of that kind.
- `not_found_activities`: count of `availability_status='not_found'`.
- `not_listed_activities`: count from a full discovery, if run.
- `note`: `"coverage reflects history accessible through the Personal API; completeness is not inferred from HTTP success alone"`.

### 6. CLI (`cli.py`)

`garmin-archive [--db PATH] update [--full] [--from YYYY-MM-DD --to YYYY-MM-DD] [--activity-id ID] [--resume RUN_ID] [--retry-failed] [--archive-only] [--recent-days N]`

- The command builds `SyncOptions`, then calls `client.update(...)` through the existing `client_factory` seam.
- Progress lines go to **stderr** in the form `discovery: page 3 (147 activities)` and `fetch: 12/630 summary 123456`. stdout contains exactly one JSON document.
- On success it prints `success(asdict(SyncResult))`, with the warnings from the result in the envelope's `warnings`. The document's top-level `status` is set to the run status, so it is `"ok"` only for `complete`, and otherwise `"partial"`/`"interrupted"`.
- Exit codes:

  | Exit | Condition |
  | --- | --- |
  | 0 | complete |
  | 6 | partial |
  | 130 | interrupted |
  | 3 | auth or account failure |
  | 4 | transport or rate-limit failure before any work |
  | 5 | storage error |
  | 2 | invalid arguments |

- **Ctrl-C** is handled with a SIGINT handler installed for the duration of the command. It sets the `cancel` event, so the library stops at the next unit boundary, finalizes as `interrupted`, and the CLI prints the JSON result and exits 130. A second Ctrl-C restores the default handler, which aborts immediately.
- If the DB is not initialized, the error envelope has code `NOT_INITIALIZED` or `DATABASE_NOT_FOUND` with a message suggesting `garmin-archive init`, and exit 5.

## Files to Modify

| File | Change |
|---|---|
| `src/garmin_archive/archive.py` | `mark_pending` (additive) |
| `src/garmin_archive/models.py`, `output.py` | `InvalidArgumentError` (exit 2) |
| `src/garmin_archive/sync/models.py`, `sync/queue.py`, `sync/runner.py`, `sync/coverage.py` | New |
| `src/garmin_archive/client.py` | `update` |
| `src/garmin_archive/cli.py` | `update` command |
| `tests/sync/test_queue.py`, `tests/sync/test_runner.py`, `tests/sync/test_coverage.py`, `tests/test_cli_update.py` | New; extend `tests/sync/fake_garmin.py` as needed |

## Acceptance Criteria

Tests use an initialized DB in `tmp_path`, the fake Garmin API (`tests/sync/fake_garmin.py`) via `client_factory`/injected `http`, a fixed controllable clock, and recording sleep. Summaries, workouts and files served by the fake are copies of the committed sanitized fixtures (`tests/fixtures/contract/summaries|workouts`) with synthetic IDs, plus in-memory ZIPs. The real API is never touched.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`, with zero errors.
2. **First update on an initialized empty DB**, with 30 activities across 4 sports, including a `made_up_sport` and a multisport parent with 2 children:
   - status `complete` and exit 0
   - every activity has summary, workouts and original_file `available`
   - 90 resource payloads, with ZIP bytes byte-identical to the fake's
   - the account is bound to the fake profile ID
   - `coverage.original_file.available == 30`
3. **Second unchanged update**, `incremental`, with the clock moved 30 days ahead so nothing is in the recent window:
   - discovery reports all unchanged
   - `resources_queued == 0`, no new `raw_payloads` rows, status `complete`
4. **Recent window:** with the clock set so that 5 activities started within 14 days, an incremental run queues exactly those 5 × 3 resources. Identical bodies count as `resources_unchanged == 15`.
5. **Changed item:** after altering one search item, the incremental run re-fetches that activity's 3 resources only. If the fake also changed its summary, a new summary payload version exists and the old bytes are intact.
6. **Missing original:** an activity whose file returns 404 ends with `original_file` `unavailable` / `NOT_FOUND`. The run is still `complete`, `coverage.original_file.unavailable == 1`, and the next incremental run does **not** re-request it. `--full` does.
7. **Summary 404:** `availability_status == 'not_found'`, nothing is deleted, and the other resources are still attempted.
8. **Transient failure:** a summary that returns 503 for all attempts ends `failed` with `next_retry_at_utc` set, and the run is `partial` with exit 6 and `resume` info. `update --retry-failed` then (with the fake healed) fetches only that resource, with no discovery request, and the run is `complete`.
9. **Rate limit:** a 429 with `Retry-After: 600` mid-loop stops the loop. Status is `partial`, the remaining resources are `pending`, and `update --resume <id>` completes them and re-checks the inventory boundary page.
10. **Account mismatch:** a DB bound to profile A, with a fake whose summaries carry profile B, gives status `failed`, exit 3 and code `ACCOUNT_MISMATCH`. That summary payload is archived, and no further resources are fetched.
11. **Auth failure:** a token endpoint 401 gives exit 3, `AUTH_FAILED`, and a run row with status `failed`.
12. **Interrupt:** setting `cancel` after 5 resources gives status `interrupted`, exit 130 from the CLI (the test drives the event through the library and checks the CLI mapping separately), pending resources remain, and resume completes them.
13. **Crash recovery:** a run row left as `running`, with pending resources (simulated by inserting the rows), is marked `interrupted` by `--resume`, and its work completes.
14. **Selectors:**
    - `--full --activity-id 1`, `--from` without `--to`, `--from 2026-07-01 --to 2026-06-01`, and `--resume 999` (no such run) each exit 2 with `INVALID_ARGUMENT`.
    - `--activity-id <id>` fetches exactly 3 resources and no search pages.
    - `--from/--to` sends `startDate`/`endDate`, and its mode is `scoped`.
15. **Not initialized:** `update` on a missing DB path gives exit 5 and code `DATABASE_NOT_FOUND`, and creates no file.
16. **Lock:** `update` while the writer lock is held gives exit 5 and `ARCHIVE_LOCKED`.
17. **No transaction during HTTP:** the fake transport asserts `conn.in_transaction is False` for every request, across all scenarios above. The fake gets the connection through a test hook.
18. **Output:** for every CLI scenario, stdout holds exactly one JSON document and progress appears on stderr only. The fake API key and token never appear in stdout, stderr, `caplog`, the DB file bytes (`Path(db).read_bytes()`, plus the WAL file if present) or `sync_runs.parameters_json`.

## Pending Decisions

- **Resolved:** `unavailable` (source-confirmed 404) and `forbidden` do not make a run partial (SPEC §9). `failed` and `pending` do.
- **Resolved:** An account mismatch keeps the offending raw payload archived (SPEC §5.1: never delete raw data) and stops the run.
- **Resolved:** The default retry delay recorded for transport failures is 1 hour (`next_retry_at_utc`). It is informational. `--retry-failed` ignores it and retries immediately.
- **Resolved:** `--archive-only` is accepted and recorded. Processing does not exist, so every run is archive-only and carries the warning.
- **Out of scope:** analytical processing, the `status`, `list-activities`, `show-activity`, `export-raw`, `validate` and `backup` commands, persistent token caching, and concurrency above 1.
