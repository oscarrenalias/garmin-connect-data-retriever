---
name: Sync and update command
id: spec-81da0774
description: null
dependencies: null
priority: high
complexity: null
status: draft
tags: []
scope:
  in: null
  out: null
feature_root_id: null
---
# Sync and update command

## Objective

Implement `garmin-archive update`. It discovers every accessible activity and archives each one's summary, workouts and original file in the local database, byte-exact, for **all** sports. It reports what is and isn't archived. See `docs/SPEC.md` §6. There is no analytical processing yet.

## Context

- This is a single-user tool that runs on a Raspberry Pi. Keep the code simple and add **no new dependencies**.
- Build on what exists:
  - the storage layer (`storage/`)
  - the raw archive writer (`archive.py`, with `ArchiveWriter` and `ArchiveReader`)
  - the Garmin API client from the "Garmin API client" spec
- Add small helpers to `archive.py` where needed, for example to write several rows in one transaction. Don't restructure it.
- Live facts (`tests/fixtures/contract/contract-report.md`):
  - The account has about 209 activities across 9 sport types.
  - Pages of 99 returned HTTP 500, while 49 worked.
  - No multisport or manual activities exist yet. Handle them anyway.

## Behaviour

### Discovery

1. **Paging.** Page through search from `start=0` until an **empty page**. A short page is not the end. Advance `start` by the number of items returned.
2. **Page-size fallback.** If a page returns 5xx, halve the page size (minimum 10) and retry the same `start`. Store the working size in `archive_metadata`, so the next run starts there.
3. **Per-page transaction.** For each page, in **one transaction**:
   - archive the page body
   - upsert each activity (ID as TEXT, parent ID, type key, owner ID)
   - link each activity to its search item
   - mark the resources that need fetching as `pending`
   - update a checkpoint (the next offset)

   A crash can therefore never leave a page half-recorded or lose queued work.
4. **What to mark pending:**
   - all three resources (summary, workouts, original file) for activities that are **new**, or whose search item JSON **changed** since last time (compare a canonical JSON hash of the item)
   - all three for activities that started within the last **14 days**
   - any resource that is `failed` or has no row yet
   - with `--full`: all three resources for every activity
5. **Stop conditions.** Detect a repeated page (the same activity IDs as the previous page) and a page ceiling (default 10,000). Stop discovery and report the inventory as **incomplete**. Duplicate IDs across pages are counted once.
6. **Account check.** If the DB is already bound to an account and a page has items with a different `ownerId`, stop with an account-mismatch error before writing that page.

### Fetching

7. Fetch every `pending` resource, one at a time.
8. **No database transaction is open during any network call.**
9. Responses:
   - **2xx:** archive the body with the writer and mark the resource `available`.
   - **Summary 2xx, extra steps:**
     - Bind the DB to its `userProfileId` on first sight (`bind_account`). If a different profile appears later, stop the run with an account-mismatch error. The payload stays archived.
     - Update the activity's type and parent from the summary.
   - **404:** mark the resource `unavailable`. For a summary, also mark the activity `not_found`. **Nothing is ever deleted.**
   - **403:** mark the resource `forbidden`.
   - **Transport error or contract error** on a single resource: mark it `failed` and continue.
   - **Rate limit:** stop fetching, leave the rest `pending`, and finish as partial.
   - **Auth failure:** stop and finish as failed.

### Runs and resume

10. Each invocation creates a `sync_runs` row. On exit it records status, counters and coverage.
11. `update --resume <run_id>` continues an interrupted or partial run:
    - It continues discovery from the checkpoint, re-fetching the boundary page, if discovery hadn't completed.
    - It then fetches everything still pending.
    - A `running` row left behind by a crash is marked `interrupted`.
12. Ctrl-C sets a stop flag that is checked between pages and resources. The run ends as `interrupted` and stays resumable. The command exits 130.

### Status and output

13. **Status:**
    - `complete`: discovery reached the empty page, and nothing ended `failed` or `pending`. `unavailable` and `forbidden` count as known outcomes, not failures.
    - `partial` or `interrupted`: otherwise.
    - `failed`: auth or account mismatch.
14. **Output:** one JSON document on stdout, with progress lines on stderr. It uses the existing envelope, and the top-level `status` is the run status. It includes:
    - **counters:** discovered / new / changed, resources downloaded, unchanged, unavailable, forbidden and failed
    - **coverage:** per resource kind, counts by state across the whole archive, plus the inventory status
    - **resume info** when the run is not complete
15. **Exit codes:**

    | Code | Meaning |
    | --- | --- |
    | 0 | complete |
    | 6 | partial |
    | 130 | interrupted |
    | 3 | auth or account failure |
    | 4 | transport, rate limit or contract failure before anything was archived |
    | 5 | storage error, missing or uninitialized DB, or the archive is locked |
    | 2 | bad arguments, e.g. `--full` together with `--resume` |

16. **Initialization.** `update` never creates or migrates a database. It tells the user to run `garmin-archive init`. It holds the writer lock for the whole run.

## Acceptance Criteria

Tests use a reusable fake Garmin API on `httpx.MockTransport` (`tests/sync/fake_garmin.py`), built from copies of the committed sanitized fixtures with synthetic IDs, plus in-memory ZIPs, and an initialized DB in `tmp_path`. No network.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check` and `pyright`.
2. **First run.** 250 activities across several sports, including an unknown type key, a multisport parent and 2 children:
   - status `complete` and exit 0
   - every activity has all 3 resources `available`
   - the files are byte-identical to the fake's
   - the DB is bound to the fake profile
3. **Second unchanged run.** With the clock moved so nothing is recent:
   - no resources fetched
   - no new payload rows
   - `complete`
4. **Change detection.** Changing one search item re-fetches only that activity. Activities started within 14 days are re-fetched, and identical bodies are de-duplicated.
5. **Page-size fallback.** A fake that 500s above 30 items per page still discovers everything, and the next run starts at the stored working size. A fake that never returns more than 40 items per page, whatever the requested limit, still discovers everything.
6. **Unavailable resources.** A file 404 ends `unavailable`, the run is still `complete`, and the next normal run doesn't re-request it. `--full` re-requests it. A summary 404 marks the activity `not_found` and deletes nothing.
7. **Partial and resume.** A summary that keeps returning 503 ends `failed`, giving a partial run with exit 6. The next `update` retries it. A rate limit mid-fetch gives a partial run, and `--resume` completes it.
8. **Interrupted discovery.** A run stopped after discovery page 2 keeps those pages' activities pending. `--resume` finishes discovery and fetching, and every activity is fetched exactly once.
9. **Account mismatch.** Summaries for another profile give exit 3, and the mismatching payload is kept.
10. **Atomic pages.** A failure injected partway through a page's writes leaves nothing from that page in the DB.
11. **No transaction during HTTP.** The fake transport asserts that no transaction is open on every request.
12. **Arguments and preconditions:**
    - `--full --resume 1` gives exit 2.
    - A missing DB gives exit 5 and creates no file.
    - A held writer lock gives exit 5.
13. **Output hygiene.** stdout is exactly one JSON document. The fake key and token never appear in stdout, stderr, logs or the DB file.

## Out of scope

- date-range sync
- single-activity sync
- `--retry-failed` (a normal `update` already retries failures)
- two-stage Ctrl-C
- concurrency above 1
- processing and the analytical tables
- `status` / `list` / `export` / `validate` / `backup` commands
