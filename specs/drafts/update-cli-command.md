---
name: update CLI command
id: spec-76990fac
description: "garmin-archive update: options, stderr progress, run-status envelope and exit codes, two-stage Ctrl-C handling"
dependencies: null
priority: high
complexity: null
status: draft
tags:
- cli
- sync
scope:
  in: null
  out: null
feature_root_id: null
---
# update CLI command

## Objective

Add `garmin-archive update`, a thin CLI over `ArchiveClient.update` from the "Sync runner" spec. It covers:

- option parsing
- stderr progress
- the JSON result envelope with the run status
- the exit-code table (`docs/SPEC.md` §9)
- Ctrl-C handling that leaves a resumable run

## Problems to Fix

1. Users and agents need a command-line entry point to synchronize. Today `ArchiveClient.update` can only be called from Python.
2. SPEC §9 requires the following, none of which exists yet:
   - exactly one JSON result on stdout, with progress on stderr
   - partial results with resume information
   - exit codes 0, 2, 3, 4, 5, 6 and 130
   - rejection of incompatible selectors
   - Ctrl-C handling with a resumable checkpoint

## Changes

### 1. Command

```
garmin-archive [--db PATH] update [--full] [--from YYYY-MM-DD --to YYYY-MM-DD]
    [--activity-id ID] [--resume RUN_ID] [--retry-failed] [--archive-only] [--recent-days N]
```

- The command builds `SyncOptions` and calls `client.update(options, cancel=event, progress=cb)`. The client is obtained through the existing `client_factory` seam in `main()`.
- Selector validation lives in the library (`InvalidArgumentError`). The CLI only parses types:
  - A bad date or a non-integer is reported as a Typer/Click usage error, which produces `INVALID_ARGUMENT` and exit 2.
  - The CLI contains no sync logic.

### 2. Output

- **`output.success` gains a keyword-only `status: str = "ok"` argument** for the top-level status. Existing callers are unchanged.
- The `update` envelope depends on the run status:

  | `SyncResult.status` | Envelope |
  |---|---|
  | `complete` | `success(asdict(result), warnings=result.warnings)`, exit 0 |
  | `partial` | `success(..., status="partial")`, exit 6 |
  | `interrupted` | `success(..., status="interrupted")`, exit 130 |
  | `failed` | `error(result.error.code, result.error.message, retryable=..., details={"run_id": ..., "result": asdict(result)})` |

- Exit codes for a `failed` run come from `exit_code_for` on the error code:
  - 3: `AUTH_FAILED`, `CREDENTIALS_NOT_FOUND`, `INSECURE_KEY_FILE`, `FORBIDDEN`, `ACCOUNT_MISMATCH`
  - 4: `TRANSPORT_ERROR`, `RATE_LIMITED`, `CONTRACT_MISMATCH`

  Add a helper `exit_code_for_code(code: str) -> int` in `output.py` that uses the same mapping as `exit_code_for`.
- Exceptions raised by `update` go through the existing `main()` handling:

  | Exception | Exit |
  |---|---|
  | `InvalidArgumentError` | 2 |
  | `DatabaseNotFoundError`, `NotInitializedError` | 5 (message suggests `garmin-archive init`) |
  | `ArchiveLockedError` | 5 |
  | `StorageError` | 5 |

- `interrupted` is a new top-level status value, alongside SPEC §9's `ok`, `partial` and `error`. Record this as a resolved decision.

### 3. Progress (stderr)

Progress lines are written to `sys.stderr` only. Formats:
- `discovery: page {done} ({n} activities)`
- `fetch: {done}/{total} {kind} {activity_id}`

stdout receives only the final JSON document.

### 4. Ctrl-C

The SIGINT handler is installed only while `update` runs, and the previous handler is restored in `finally`.

- **First SIGINT:** set the `cancel` event, write `interrupt received; finishing current request (press Ctrl-C again to stop immediately)` to stderr, and switch the handler to `signal.default_int_handler`.
- **Second SIGINT:** `default_int_handler` raises `KeyboardInterrupt`. The library catches it, finalizes the run as `interrupted` and returns. The CLI emits the interrupted envelope and exits 130.
- If a `KeyboardInterrupt` escapes anyway (for example, before the run row exists), `main()` emits `error("INTERRUPTED", "interrupted")` and exits 130. stdout must still contain exactly one JSON document.
- Cancellation latency is bounded by the in-flight request's retry budget. This is documented in `--help`.

## Files to Modify

| File | Change |
|---|---|
| `src/garmin_archive/cli.py` | `update` command, SIGINT handling, `KeyboardInterrupt` → JSON + 130 in `main()` |
| `src/garmin_archive/output.py` | `success(status=...)`, `exit_code_for_code` |
| `tests/test_cli_update.py` | New |

## Acceptance Criteria

Tests call `main([...], client_factory=...)`, which builds a client on the fake Garmin API (`tests/sync/fake_garmin.py`) with an initialized `tmp_path` DB, and capture stdout and stderr with `capsys`.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright` with zero errors.
2. Status mapping:
   - A complete run gives exit 0 and `"status": "ok"`. `data` contains `run_id`, `counters` and `coverage`, and `warnings` includes the archive-only warning.
   - A partial run (forced via a fake 503 on one summary) gives exit 6, `"status": "partial"`, and `data.resume.run_id` present.
   - A failed run with `ACCOUNT_MISMATCH` gives exit 3, `"status": "error"`, and `error.details.run_id` present.
   - A failed run with `RATE_LIMITED` on page 1 gives exit 4.
3. Argument errors: each of these gives exit 2 and code `INVALID_ARGUMENT`, and leaves no `sync_runs` row:
   - `update --full --activity-id 1`
   - `update --from 2026-07-01`
   - `update --from 2026-07-01 --to 2026-06-01`
   - `update --from notadate --to 2026-06-01`
   - `update --resume 999`
4. Preconditions:
   - A missing DB gives exit 5 and `DATABASE_NOT_FOUND`, the message mentions `garmin-archive init`, and no file is created.
   - A held writer lock gives exit 5 and `ARCHIVE_LOCKED`.
5. Progress:
   - stderr contains `discovery: page` and `fetch:` lines.
   - stdout parses as exactly one JSON document and contains no progress text.
6. Real signal:
   - **Single SIGINT:** the fake transport calls `signal.raise_signal(signal.SIGINT)` once during the fetch phase. The result is exit 130, `"status": "interrupted"`, exactly one JSON document, the run row `interrupted`, and the remaining resources still `pending`.
   - **Double SIGINT:** raising it twice in succession gives the same outcome.
   - Afterwards `update --resume <id>` completes with exit 0.
   - The test is skipped on Windows.
7. Handler restore: after `update` returns, `signal.getsignal(SIGINT)` equals the handler that was installed before the call.
8. Hygiene: the fake key and token never appear in stdout or stderr in any scenario.

## Pending Decisions

- **Resolved:** `interrupted` is an additional top-level envelope status, used with exit 130.
- **Resolved:** For failed runs, the full `SyncResult` is carried in `error.details.result`, so automation still gets counters and coverage.
- **Out of scope:** human-readable output formats and the `status` command.
