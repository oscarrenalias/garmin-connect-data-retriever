---
name: "Sync runner: durable fetch queue, runs and coverage"
id: spec-7173e307
description: "ArchiveClient.update: run records, durable per-page fetch queue, fetch handling for all sports, account binding, resume/retry/activity modes, outcome table, counters and coverage"
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
# Sync runner: durable fetch queue, runs and coverage

## Objective

Complete library-level synchronization (`docs/SPEC.md` §6) in `ArchiveClient.update(SyncOptions) -> SyncResult`. This covers:

- run records
- a durable fetch queue built atomically with each discovered page
- fetching and archiving summary, workouts and original file for **every** sport
- account binding from verified summaries
- resume, retry-failed and single-activity modes
- counters and coverage

The CLI command lives in the follow-up spec "update CLI command". Analytical processing does not exist yet, so every run is effectively archive-only.

Depends on merged code: storage foundation, raw archive writer, credentials, HTTP adapter, and sync discovery (`run_discovery` with its `on_page` hook, `ClassifiedItem`, `DiscoveryScope`, `tests/sync/fake_garmin.py`).

## Problems to Fix

1. SPEC §6.1 requires summary, workouts and original-file retrieval for every discovered activity, with each response persisted before any processing. Empty workout arrays count as complete. Missing originals must appear in coverage without blocking anything.
2. SPEC §6.2 defines the incremental rules (new, changed, missing or failed, recent window of 14 days), `--full` re-fetch, and the scoped, single-activity, resume and retry-failed modes. A resumed run must not lose the original run's queue, so the queue must be durable before the network work it describes.
3. SPEC §4 requires binding the database to `userProfileId` from a verified summary and rejecting other profiles. An empty account stays unverified.
4. SPEC §6.2 and §9 require counters, separate summary and original-file coverage, partial results with resume information, and resumable interruption.

## Changes

### 1. Writer additions (`archive.py`, additive; existing tests unchanged)

- `mark_pending_in(conn, *, activity_id, kinds, now_utc) -> list[str]` is a module function. It **assumes an open transaction** and upserts `state='pending'` for each kind in `summary`, `workouts` and `original_file`. It keeps `current_payload_id`, `last_success_at_utc` and `json_pointer`, and clears `error_code` and `next_retry_at_utc`. It returns the kinds it marked.
- `ArchiveWriter.mark_pending(*, activity_id, kinds) -> list[str]` wraps `mark_pending_in` in one transaction.
- `record_response(...)` gains an optional keyword argument `error_code: str | None = None`. It is stored on the `activity_resources` row in the same transaction, which lets a response be archived with `resource_state='failed', error_code='CONTRACT_MISMATCH'` atomically. The default `None` keeps existing behaviour.
- `ArchiveWriter.mark_summary_check` already accepts `forbidden`.

### 2. Options, errors and result (`sync/models.py`, `models.py`, `output.py`)

- Add `InvalidArgumentError(GarminArchiveError)` with code `INVALID_ARGUMENT`, mapped to exit 2 in `output.exit_code_for`.
- `SyncOptions` is a frozen dataclass with these fields:

  | Field | Default |
  | --- | --- |
  | `full` | `False` |
  | `from_date`, `to_date` | `None` |
  | `activity_id` | `None` |
  | `resume_run_id` | `None` |
  | `retry_failed` | `False` |
  | `archive_only` | `False` |
  | `recent_window_days` | `14` |

- `validate(options, conn)` runs **before any `sync_runs` row is inserted**. It raises `InvalidArgumentError` when:
  - more than one selector is set (`full`, the date pair, `activity_id`, `resume_run_id`, `retry_failed`)
  - only one of `from_date` / `to_date` is given
  - `from_date > to_date`
  - `activity_id` contains non-digits
  - `recent_window_days < 0`
  - `resume_run_id` does not exist
  - `resume_run_id` refers to a run whose status is `complete`
- Mode mapping:

  | Option set | Mode |
  | --- | --- |
  | `full` | `full` |
  | none | `incremental` |
  | dates | `scoped` |
  | `activity_id` | `activity` |
  | `resume_run_id` | `resume` |
  | `retry_failed` | `retry_failed` |

- `SyncResult` is a frozen dataclass that serializes with `asdict`:
  - `run_id`, `mode`, `root_mode` (for resumes, the mode of the root run)
  - `status`: one of `complete`, `partial`, `failed`, `interrupted`
  - `error`: `{code, message, retryable}` when `status == failed`, else `None`
  - `started_at_utc`, `finished_at_utc`
  - `discovery`: the `DiscoveryResult` as a dict, or `None`
  - `counters`, `coverage` (see §6)
  - `failures`: a list of `{activity_id, kind, error_code, http_status}`, capped at 100, plus `failures_truncated`
  - `resume`: `{"run_id": n}` when status is `partial` or `interrupted`
  - `warnings`: a list of strings that always includes `"analytical processing is not available in this version; archive only"`

### 3. Queue rules (`sync/queue.py`)

**The queue is the set of `activity_resources` rows in state `pending`.** No queue lives only in memory.

**Per-page marking.** For `full`, `incremental` and `scoped` (and their resumes), the runner passes an `on_page` hook to `run_discovery`. The hook runs inside the page transaction and calls `mark_pending_in` for each `ClassifiedItem`:

| Mode | Kinds marked per item |
| --- | --- |
| `full` | all three kinds |
| `incremental`, `scoped` | all three if `classification` is `new` or `changed`, or if the item's local start date (`start_time_local[:10]`) is `>= clock.now().date() - recent_window_days` (UTC date, inclusive bound). Otherwise, only kinds whose current state is `failed` or that have no row. `unavailable`, `forbidden` and `available` kinds are left alone. |

Only activities seen on that page are affected. For `scoped` mode, the failed and absent re-queue is therefore limited to the window.

**Modes without discovery:**

| Mode | Behaviour |
| --- | --- |
| `activity` | Before fetching, mark all three kinds pending for that activity. If no `activity_archive` row exists, first insert one via `upsert_activity` with nulls. |
| `retry_failed` | Before fetching, mark every resource in state `failed` (summary, workouts, original_file) pending, across all activities. |

**Resume.**
1. Follow `parameters_json.resumes_run_id` back to the **root** run, i.e. the first run in the chain that is not a resume.
2. If the run being resumed is still `running` (a crashed process), set it to `interrupted`.
3. If the root mode used discovery (`full`, `incremental`, `scoped`), run discovery with the root's scope, `resume=True`, and the root mode's per-page rules. The root's `full` and `recent_window_days` values come from its `parameters_json`.
4. Then process pending resources.
5. For root modes `activity` and `retry_failed`, do not run discovery. Process pending only (`activity`: that activity's pending resources).

**Fetch scope.**

| Mode | Pending resources fetched |
| --- | --- |
| `activity` | only that activity's |
| all other modes | **all** pending resources in the archive |

Fetch order: `activity_id` ascending, then `summary`, `workouts`, `original_file`.

### 4. Fetch handling (`sync/runner.py`)

For each pending resource:

1. Check `cancel` first. If it is set, stop with status `interrupted`.
2. Call the adapter with no transaction open.
3. Handle the response:

**2xx summary.** Parse the JSON **before** recording:
- If it is not an object, or `str(activityId)` differs from the requested ID: `record_response(resource_state='failed', error_code='CONTRACT_MISMATCH')`.
- If `userProfileId` is a non-null integer or digit string:
  - **No binding yet:** `record_response` with `available`, then `bind_account` (it binds).
  - **Binding matches:** `record_response` with `available`.
  - **Binding differs:** `record_response(resource_state='failed', error_code='ACCOUNT_MISMATCH')`, then raise `AccountMismatchError` (see §5). The payload stays archived (SPEC §5.1).
- If `userProfileId` is missing or null: `record_response` with `available`, skip binding, and add the warning `"account binding pending: summary without userProfileId"`.
- After an `available` record:
  - `upsert_activity(owner_profile_id=userProfileId-as-str-or-None, parent_activity_id=parentId, type_key=activityTypeDTO.typeKey or the stored value)`
  - `mark_summary_check(availability_status='listed')`

**2xx workouts.** A JSON array, including `[]`, is recorded `available`. Anything else is recorded `failed` with `CONTRACT_MISMATCH`.

**2xx original_file.** Recorded `available` as-is, whatever the content type.

**404.** `record_failure(state='unavailable', error_code='NOT_FOUND', http_status=404)`. For a summary, also call `mark_summary_check('not_found')`. Nothing is ever deleted.

**403 (`AuthorizationError`).** `record_failure(state='forbidden', error_code='FORBIDDEN')`. For a summary, also call `mark_summary_check('forbidden')`. Continue.

**Other errors:**

| Error | Recorded failure | Then |
| --- | --- | --- |
| `TransportError` | `failed`, `TRANSPORT_ERROR`, `next_retry_at_utc = now + 1h` | continue |
| `ContractMismatchError` | `failed`, `CONTRACT_MISMATCH` | continue |
| `RateLimitedError` | `failed`, `RATE_LIMITED`, `next_retry_at_utc = now + retry_after_s` (or `now + 1h` if `retry_after_s is None`) | stop fetching; see §5 |

**`AuthFailedError`, `CredentialsNotFoundError`, `InsecureKeyFileError`.** Stop; see §5.

### 5. Outcome table (normative)

`update` **raises** only before a run row exists:
- `InvalidArgumentError`
- `DatabaseNotFoundError` / `NotInitializedError` (via `check_schema`; `update` never creates or migrates)
- `ArchiveLockedError`

Once the run row exists, `update` always finalizes the run and **returns** a `SyncResult`, except for `StorageError`, which is finalized best-effort as `failed` and then re-raised.

| Source | Error | Run status | `SyncResult.error.code` | Fetching continues? |
|---|---|---|---|---|
| token acquisition, any phase | `CredentialsNotFoundError`, `InsecureKeyFileError`, `AuthFailedError` | `failed` | that code | no |
| token acquisition | `TransportError` | `failed` | `TRANSPORT_ERROR` | no |
| discovery | `AuthFailedError`, `AccountMismatchError`, `AuthorizationError` | `failed` | that code | no |
| discovery | `ContractMismatchError` | `failed` | `CONTRACT_MISMATCH` | no |
| discovery | `RateLimitedError`, or `TransportError` at the minimum page size, **before any page committed** | `failed` | that code | no |
| discovery | same errors **after** at least one page committed | `partial` | `None` | yes, for already-pending resources |
| fetch | `AccountMismatchError` | `failed` | `ACCOUNT_MISMATCH` | no |
| fetch | `AuthFailedError` | `failed` | `AUTH_FAILED` | no |
| fetch | `RateLimitedError` | `partial` | `None` | no; the remaining resources stay pending |
| any | cancel event or `KeyboardInterrupt` | `interrupted` | `None` | no |
| none of the above | — | `complete` if discovery was `complete` (or skipped) **and** no resource ended `failed` or `pending`; otherwise `partial` | `None` | — |

`unavailable` and `forbidden` resources never make a run partial (SPEC §9). They appear in coverage.

**`KeyboardInterrupt`.** It is caught inside `update` and finalized as `interrupted`, and `update` **returns** the result rather than re-raising. The CLI maps this to exit 130. Cancellation takes effect between work units. A request already in flight may take up to its retry/backoff budget (≤ `max_retry_after_s`) before the check runs, which is accepted.

### 6. Finalize, counters and coverage (`sync/coverage.py`)

**Finalize** in one transaction:
- Set `finished_at_utc`, `status`, `counters_json`, and `coverage_json`. `coverage_json` includes `inventory_status`.
- Set `archive_metadata['last_sync'] = {run_id, mode, status, finished_at_utc}`.
- If mode (or root mode) is `full` and status is `complete`, set `archive_metadata['last_full_refresh_at_utc']`.

**Counters** (this run):

| Counter | Meaning |
| --- | --- |
| `activities_discovered`, `activities_new`, `activities_changed`, `activities_unchanged` | 0 without discovery |
| `resources_queued` | Count of kinds marked pending by this run |
| `resources_downloaded` | 2xx that created a new payload version |
| `resources_unchanged` | 2xx deduped to an existing payload |
| `resources_unavailable`, `resources_forbidden`, `resources_failed` | Per outcome |
| `resources_remaining_pending` | Pending resources left at the end |
| `activities_processed` | 0 |

**Coverage** (whole archive after the run, computed with SQL):

| Key | Meaning |
| --- | --- |
| `activities_total` | |
| `inventory_status` | This run's discovery status (`complete`, `incomplete`, `interrupted`), or `provisional` when complete but `discovery.provisional` is set. Without discovery in this run, the value from the latest `sync_runs.coverage_json` that has one, else `unknown`. |
| `last_full_refresh_at_utc` | |
| `summary`, `workouts`, `original_file` | Each `{available, unavailable, forbidden, failed, pending, missing}`. `missing` means activities with no row of that kind. |
| `not_found_activities` | |
| `not_listed_activities` | The count from this run's full-scope discovery, else `null` |
| `note` | `"coverage reflects history accessible through the Personal API; completeness is not inferred from HTTP success alone"` |

### 7. `ArchiveClient.update`

`update(options, *, cancel: threading.Event | None = None, progress: Callable[[ProgressEvent], None] | None = None) -> SyncResult`. It:

1. Validates.
2. Runs `check_schema` via the read path.
3. Acquires `writer_lock` for the whole run.
4. Opens a write connection.
5. Inserts the `sync_runs` row (`status='running'`, `parameters_json` = the options with ISO dates, plus `resumes_run_id` for resumes).
6. Runs discovery and the fetch loop.
7. Finalizes.
8. Closes the connection and releases the lock in `finally`.

Additional rules:

- `ProgressEvent(phase: Literal["discovery", "fetch"], done: int, total: int | None, activity_id: str | None, kind: str | None)` is emitted after each page and each resource.
- HTTP concurrency is 1.
- No SQLite transaction is open during any adapter call.
- Library code never prints.

## Files to Modify

| File | Change |
|---|---|
| `src/garmin_archive/archive.py` | `mark_pending_in`, `mark_pending`, `record_response(error_code=...)` (additive) |
| `src/garmin_archive/models.py`, `output.py` | `InvalidArgumentError` (exit 2) |
| `src/garmin_archive/sync/models.py`, `sync/queue.py`, `sync/runner.py`, `sync/coverage.py` | New |
| `src/garmin_archive/client.py`, `__init__.py` | `update`; re-export `SyncOptions`, `SyncResult`, `ProgressEvent` |
| `tests/sync/test_queue.py`, `tests/sync/test_runner.py`, `tests/sync/test_coverage.py`, `tests/test_archive.py` (additions) | New/extended; extend `tests/sync/fake_garmin.py` as needed |

## Acceptance Criteria

Test setup:
- an initialized DB in `tmp_path`
- `ArchiveClient` built with injected `http` (the fake Garmin API from `tests/sync/fake_garmin.py`), a fake credential provider, a controllable clock and recording sleep
- summaries and workouts are copies of `tests/fixtures/contract/summaries|workouts` with synthetic IDs
- files are in-memory ZIPs
- no network

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright` with zero errors. Existing tests still pass.
2. **First update** (30 activities across 4 sports, including a `made_up_sport` and a multisport parent with 2 children):
   - `status == "complete"`
   - every activity has all 3 kinds `available`
   - exactly 90 non-`search_page` `raw_payloads` rows
   - ZIP blobs are byte-identical to the fake's
   - the account is bound to the fake profile
   - `coverage.original_file.available == 30`
3. **Unchanged incremental** (clock +30 days, so nothing is in the recent window): all items `unchanged`, `resources_queued == 0`, no new `raw_payloads` rows, `complete`.
4. **Recent window:** 5 activities whose start date is ≥ today − 14 (one exactly on the boundary date) → exactly 15 resources queued, and `resources_unchanged == 15`.
5. **Changed item:** one activity's 3 resources are re-fetched. A changed summary body creates a new version, and the old bytes are intact.
6. **Missing original:**
   - A file 404 leaves `original_file` `unavailable/NOT_FOUND`, the run `complete`, and `coverage.original_file.unavailable == 1`.
   - The next incremental run does not re-request it. `full` does.
7. **Summary 404 / 403:** `availability_status` is `not_found` / `forbidden`, nothing is deleted, and the other kinds are still attempted.
8. **Transient failure:** a summary returning 503 on every attempt ends up `failed`, with `next_retry_at_utc` set and the run `partial` with `resume`. `retry_failed` (fake healed) then makes no search requests, fetches only that resource, and completes.
9. **Rate limit mid-fetch** (`Retry-After: 600`): `partial`, the remaining resources stay `pending`, and `resume` completes them without re-queuing anything else.
10. **Interrupted discovery keeps the queue:**
    - A `full` run is cancelled after discovery page 2 (of 4): pages 1–2's activities are all pending.
    - `resume` continues discovery from the boundary using full-mode rules, ends with every activity's 3 kinds fetched, and reports `root_mode == "full"`.
    - A resume of that resume follows the chain to the same root.
11. **Account mismatch:**
    - With a DB bound to A and summaries carrying B: `status == "failed"` and `error.code == "ACCOUNT_MISMATCH"`.
    - That summary is archived with resource state `failed/ACCOUNT_MISMATCH`, and no further requests are made.
    - A summary with null `userProfileId` on an unbound DB archives, does not bind, and adds the warning.
12. **Contract mismatch:** a summary whose `activityId` differs from the request is archived and the resource is `failed/CONTRACT_MISMATCH`. Workouts returning `{}` gives the same outcome.
13. **Auth:**
    - A token 401 gives `failed/AUTH_FAILED` with the run row finalized.
    - No credentials gives `failed/CREDENTIALS_NOT_FOUND`.
14. **Discovery outcomes:**
    - A 429 (600 s) on page 1 gives `failed/RATE_LIMITED`.
    - The same 429 on page 3 gives `partial`, and pages 1–2's pending resources are fetched.
    - A non-array page gives `failed/CONTRACT_MISMATCH`.
15. **Validation before insert:**
    - Each of these raises `InvalidArgumentError` and creates **no** `sync_runs` row: `full` + `activity_id`; `from_date` alone; `from > to`; `resume_run_id=999`; resuming a `complete` run.
    - `activity_id` mode fetches exactly 3 resources and no search pages.
    - Dates produce mode `scoped`, with `startDate`/`endDate` sent.
16. **Preconditions:** a missing DB raises `DatabaseNotFoundError` and creates no file. A held lock raises `ArchiveLockedError`.
17. **Crash recovery:** a `running` run row plus pending rows (inserted directly) is marked `interrupted` by `resume`, and its work completes.
18. **`KeyboardInterrupt`:** one raised from inside the fake transport during fetch returns `SyncResult(status="interrupted")` with the run finalized. A subsequent `resume` completes the work.
19. **No transaction during HTTP:** the fake asserts `conn.in_transaction is False` on every request, across all scenarios. The connection is reachable via a test hook.
20. **Secrets:** the fake key and token never appear in `SyncResult` (serialized), `caplog`, `sync_runs` rows, or DB/WAL file bytes.

## Pending Decisions

- **Resolved:** The queue is durable and marked inside each discovery page transaction, so resume can never lose the original run's intent.
- **Resolved:** `update` returns `SyncResult` for every finalized run, including `failed`. It raises only for pre-run errors and `StorageError`.
- **Resolved:** An account-mismatch or contract-mismatch response is archived with resource state `failed`. Raw data is never discarded.
- **Resolved:** Transport failures get a recorded `next_retry_at_utc` of 1 h. It is informational, and `retry_failed` ignores it.
- **Out of scope:** the CLI (next spec), processing, and the status/list/show/export/validate/backup commands.
