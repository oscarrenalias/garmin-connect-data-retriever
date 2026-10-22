---
name: Project scaffold and Garmin contract discovery tool
id: spec-728a3079
description: "Python project scaffold (uv, ruff, pyright, pytest, Typer) plus a read-only dev tool that records the real Garmin Personal API contract and writes sanitized fixtures (SPEC §2.3)"
dependencies: null
priority: high
complexity: null
status: done
tags:
- scaffold
- discovery
- contract
scope:
  in: null
  out: null
feature_root_id: B-bc9d1f10
---
# Project scaffold and Garmin contract discovery tool

## Objective

Create the Python project skeleton for `garmin_archive` and a developer-only, read-only **contract discovery tool**. Run against the user's own Garmin account, the tool records what the Garmin Personal API actually returns and writes a human-reviewable contract report plus sanitized JSON fixtures. This satisfies the "implementation discovery gate" in `docs/SPEC.md` §2.3 before any production HTTP adapter is written.

## Problems to Fix

1. The repository has no code, packaging, test harness or lint/type tooling. takt's merge gate (`uv run pytest`) cannot run.
2. `docs/SPEC.md` §2.1–§2.3 lists contract facts the published OpenAPI document does not establish:
   - token response field names
   - the auth header convention for data calls
   - the effective page-size cap and ordering stability
   - multisport parent/child file behavior
   - manual-activity file behavior
   - rate-limit headers

   The production auth, HTTP adapter and sync specs must not guess these (SPEC §2.1: "Do not invent fields").
3. Later processor specs need realistic but **sanitized** summary/workout JSON fixtures and a structural inventory of real FIT/ZIP contents (message types and field names). No real personal data may enter git (SPEC §12, CLAUDE.md).

## Changes

### Part A — Project scaffold

- `pyproject.toml`:
  - distribution name `garmin-archive`, import package `garmin_archive`, `src/` layout
  - `requires-python = ">=3.11"`, build backend `hatchling`
  - runtime dependencies: `httpx`, `typer`, `garmin-fit-sdk`
  - a `[dependency-groups] dev` group with `pytest`, `ruff`, `pyright`
  - console script `garmin-archive = "garmin_archive.cli:app"`
- Tool configuration in `pyproject.toml`:
  - ruff: target py311, line length 100, lint rule sets `E, F, W, I, UP, B, SIM`; ruff format
  - pyright: `strict` for `src/garmin_archive`, `standard` for `tests`, pythonVersion 3.11
  - pytest: `testpaths = ["tests"]` and a registered `live` marker
- `tests/conftest.py`:
  - Skip tests marked `live` unless `GARMIN_LIVE_TESTS=1`.
  - An **autouse** fixture deletes `GARMIN_API_KEY` and `GARMIN_CONNECT_API_KEY` from the environment (`monkeypatch.delenv`) and `chdir`s into `tmp_path`. This guarantees a default `./.env` can never resolve to the repository's real `.env`.
- Test helpers build ZIP and FIT test data in memory or in `tmp_path`. `*.fit` and `*.zip` are gitignored and must never be committed.
- `garmin-fit-sdk` ships without type stubs. Wrap it behind a thin typed adapter at a single import site. A narrowly scoped `# pyright: ignore[reportMissingTypeStubs]` (or a minimal local stub under `typings/`) there is the accepted approach; no blanket ignores.
- `src/garmin_archive/__init__.py` exposes `__version__`, read from package metadata.
- `src/garmin_archive/cli.py` is a Typer app with a working `--version` option and `--help`. Production commands are out of scope.
- `uv.lock` is committed.

### Part B — Contract discovery tool

- Location: `src/garmin_archive/devtools/discovery/`.
- Invocation: `uv run python -m garmin_archive.devtools.discovery capture [options]`.
- This is a developer tool, not the production CLI/library API, but the project rules still apply:
  - The logic lives in importable modules. `__main__.py` only parses arguments, calls the logic, prints a short summary and sets the exit code.
  - The HTTP transport, clock, sleep function and credential source are injectable, so everything is testable with `httpx.MockTransport` and no network.

#### B1. Credentials

- Read the API key from environment variable `GARMIN_API_KEY`, falling back to `GARMIN_CONNECT_API_KEY`.
- If neither is set, read the same two names, with the same precedence, from a dotenv-style file:
  - The path is `--env-file PATH`, defaulting to `./.env` if it exists.
  - Support simple `KEY=value` lines, optional surrounding single or double quotes, `#` comments and blank lines.
  - No new dependency.
- The key is **never** accepted as a CLI argument.
- None of the following may be written anywhere: the key, the access token, the raw token response body, `Authorization` header values. That covers stdout, stderr, logs, the report, fixtures, the raw capture directory and exception messages.

#### B2. Token acquisition and header convention (SPEC §2.1)

- `POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token` with header `Authorization: ApiKey <key>`.
- On a non-2xx token response, record only the status and content type, then stop (outcome `stopped`, see B6).
- Record only the **shape** of the token response. Never record the body, not even locally:
  - HTTP status, content type and response header names.
  - If the body is a JSON object: each top-level key with its JSON type. For strings, record only the length and whether the value is a dot-separated 3-segment string (`jwt_like`).
  - Otherwise: the body length.
  - For keys whose names contain `expir` or `ttl` (case-insensitive), also record the value when it is numeric.
- Token field selection:
  - `--token-field NAME` names the field explicitly.
  - Without it, auto-select only if exactly one top-level string field's key contains `token` (case-insensitive) and contains neither `refresh` nor `type`.
  - The report records the field used and how it was chosen (`explicit` or `auto`).
  - If no field can be determined, stop with outcome `needs_token_field`. The report and stdout tell the user to rerun with `--token-field`.
- Probe the data-call header convention with one search request (`limit=1`) using `Authorization: Bearer <token>`.
  - Record the status.
  - On 401/403, record it and stop with outcome `stopped`.
  - Do not try other schemes automatically.

#### B3. Search pagination (SPEC §2.2, §6.1)

- Enumerate the full inventory with `GET /activitylist-service/activities/search/activities`.
  - Parameters: `start`, `limit=<--page-size>` (default 99), `excludeChildren=false`, `sortBy=startLocal`, `sortOrder=asc`. No other filters.
  - Advance `start` by the number of items actually returned. Stop on an empty page.
- `--max-pages` (default 500) is a safety ceiling.
  - Reaching it sets the inventory status to `incomplete` and the run outcome to `partial`, never complete.
  - B4 sampling still runs on the partial inventory.
- Page-size cap probe: request `start=0` with `limit=200`, then with `limit=1000`, and record the item counts returned.
- Ordering stability probe: at the end, re-fetch the first page and compare its activity-ID sequence with the first fetch.
- Recorded inventory facts. Raw activity IDs never appear in the report; only counts and booleans.
  - per-page item count and allowlisted headers
  - count of duplicate activity IDs across pages
  - count of items sharing an identical `startTimeLocal` with another item
  - total distinct activities
  - counts by `activityType.typeKey` (and by `parentTypeId`)
  - count of items with a non-null `parentId`
  - count of items with `isParent` or `parent` true
  - count of items with `manualActivity` or `isManualActivity` true
  - oldest and newest `startTimeLocal` as **year-month only**
  - `owner_id_constant` (boolean)
  - from the ordering probe: `stable` (boolean) and the number of differing positions

#### B4. Sample selection and per-activity resources

Selection is deterministic. Evaluate categories in this order and assign each activity to the **first** matching category only:

1. `explicit` — IDs from repeatable `--activity-id ID`. These do not count toward N. An explicit ID absent from the inventory is still fetched; its `search-items` fixture is skipped and noted in the report.
2. `multisport` — parent items (`isParent` or `parent` true). For each selected parent, also fetch **all** its children found in the inventory by `parentId`, in addition to N. Children are named `multisport-<n>-child-<k>`, with k ordered by ascending `startTimeLocal`, then activity ID.
3. `manual` — manual flag true.
4. `pool_swim` — `typeKey == "lap_swimming"`.
5. `open_water_swim` — `typeKey == "open_water_swimming"`.
6. `running` — `typeKey` contains `running`.
7. `cardio` — `typeKey` in the cardio set. Set it with repeatable `--cardio-type KEY`. Default: `cardio`, `indoor_cardio`, `hiit`, `stair_climbing`, `elliptical`, `strength_training`.
8. `other` — the most frequent remaining `typeKey`, with ties broken alphabetically.

Further selection rules:
- Multisport children are excluded from categories 3–8.
- Within a category, pick the first `--per-category N` (default 2) matches in inventory order: ascending `startTimeLocal`, then ascending activity ID.
- `<n>` is 1-based.
- A category with no match gets the finding status `not_observed` and the note "not present in account".

For each selected activity, fetch:
- `GET /activity-service/activity/{id}`
- `GET /activity-service/activity/{id}/workouts`
- `GET /download-service/files/activity/{id}`

For each response, record: status, content type, byte length, whether `ETag`/`Last-Modified` are present, and allowlisted headers.

Analyze the original-file response **in memory**. Never extract to disk.
- If it is a ZIP, list each member with:
  - its sanitized name: every run of 6+ digits is replaced by that number's run pseudonym from B7, so `<activityId>_ACTIVITY.fit` becomes `<pseudonym>_ACTIVITY.fit`
  - compressed and uncompressed size
  - compression method
- Enforce these limits. Exceeding one is recorded as a finding, not a crash:
  - at most 64 entries
  - at most 256 MiB total uncompressed
  - no members with absolute paths or `..` segments
- For each `.fit` member, decode with `garmin-fit-sdk` and record:
  - message type names with message counts
  - field names seen per message type (no values)
  - SDK decode errors
  - the number of `session`, `lap`, `length` and `record` messages
- For non-FIT members, record only the extension and size.

#### B5. HTTP behavior

- Base URL `https://personal.connectapi.garmin.com`.
- `follow_redirects=False`. A 3xx is recorded with only the target **host**, and that resource is not followed.
- Timeouts: connect 10 s, read 60 s.
- Pacing: `--delay SECONDS` (default 1.0) between distinct requests. It is not added on top of a Retry-After or backoff wait. All waiting goes through the injected sleep function.
- 429: if `Retry-After` (seconds or HTTP-date) is ≤ 120 s, wait that long and retry once. Stop the run with outcome `partial` if a second 429 arrives for the same request, or if `Retry-After` is missing or > 120 s.
- Network errors and 5xx: up to 3 retries (4 attempts total), with exponential backoff of 1 s, 2 s and 4 s. These rules apply to the token POST and to data calls.
  - If the retries are exhausted on a data call, record the failure and continue with the next resource.
  - If they are exhausted on the token POST, stop with outcome `stopped`.
- Header allowlist for value recording: `Content-Type`, `Content-Length`, `ETag`, `Last-Modified`, `Retry-After`, `Date`, and any header whose name contains `ratelimit` or `rate-limit` (case-insensitive). For other response headers, record the names only. Never record `Set-Cookie` values.

#### B6. Outputs, report schema and exit codes

**Raw capture (never committed).** Written to `.local/discovery/<run-id>/raw/`, where `<run-id>` is the UTC timestamp `YYYYMMDDTHHMMSSZ`. It contains:
- every data-call response body as exact bytes (search pages, summaries, workouts, files)
- `requests.jsonl`: method, path, query params, status, allowlisted headers and elapsed ms

It never contains the token response or auth headers. `.local/` is already gitignored. Raw files are **not** sanitized; they stay local.

**Sanitized fixtures (reviewable, intended for commit).** Written under `--fixtures-dir`, default `tests/fixtures/contract/`:
- `contract-report.json` (schema below)
- `contract-report.md`: a human-readable rendering of the same data, including a table of SPEC §2.3 items 1–6 with their status
- `summaries/<name>.json`, `workouts/<name>.json` and `search-items/<name>.json` (the matching list item), where `<name>` is `<category>-<n>` or `multisport-<n>-child-<k>`
- `files/<name>.manifest.json`: the ZIP/FIT structural inventory from B4
- `redaction-log.json`: the redacted JSON paths and the rule applied, per fixture. It contains no original values.

`contract-report.json` has this structure. Every finding object includes `status`, which is `confirmed` (directly observed in this run), `not_observed` (looked for, absent) or `unknown` (not determinable by this tool), plus an optional `note`.

```json
{
  "report_version": 1,
  "run_id": "20261022T130000Z",
  "outcome": "complete | partial | stopped | needs_token_field",
  "token": {"status": "...", "http_status": 200, "content_type": "...", "header_names": [], "fields": {"<key>": {"type": "string", "length": 0, "jwt_like": false}}, "expiry_fields": {"<key>": 3600}, "token_field": "<key>", "token_field_source": "auto | explicit"},
  "header_convention": {"status": "...", "scheme": "Bearer", "probe_http_status": 200},
  "pagination": {"status": "...", "inventory_status": "complete | incomplete", "page_size_requested": 99, "pages": [{"item_count": 99}], "duplicate_id_count": 0, "same_start_time_count": 0, "total_distinct": 0},
  "page_size_cap": {"status": "...", "probes": [{"requested": 200, "returned": 0}, {"requested": 1000, "returned": 0}]},
  "ordering": {"status": "...", "stable": true, "differing_positions": 0},
  "inventory": {"status": "...", "by_type_key": {"running": 0}, "by_parent_type_id": {"17": 0}, "child_count": 0, "parent_count": 0, "manual_count": 0, "oldest_month": "YYYY-MM", "newest_month": "YYYY-MM", "owner_id_constant": true},
  "samples": [{"name": "running-1", "category": "running", "type_key": "running", "summary": {"http_status": 200, "byte_length": 0}, "workouts": {"http_status": 200, "item_count": 0}, "original_file": {"http_status": 200, "content_type": "...", "byte_length": 0, "manifest": "files/running-1.manifest.json"}}],
  "categories": {"running": {"status": "confirmed | not_observed", "selected": 0}},
  "http": {"status": "...", "rate_limit_headers_seen": [], "retry_after_seen": false, "status_429_count": 0, "redirects": [{"host": "..."}], "other_header_names": [], "failures": [{"resource": "summary:running-1", "error": "..."}]},
  "leak_check": {"status": "...", "failed_fixtures": [{"file": "...", "json_path": "..."}]},
  "spec_2_3": {"1_token_and_header": "...", "2_pagination": "...", "3_history": "...", "4_detail_and_zip_by_sport": "...", "5_manual_multisport_type_keys": "...", "6_rate_limits_and_missing_files": "..."}
}
```

The `spec_2_3` entries are statuses, derived as follows. Any condition not met yields `unknown` unless stated otherwise.
1. Token and header: `confirmed` when the token field and Bearer probe both succeed.
2. Pagination: `confirmed` when the inventory is complete and the ordering probe ran.
3. History: always `unknown`. The oldest month is recorded in `inventory`, but historical restrictions cannot be proven by this tool.
4. Detail and ZIP by sport: `confirmed` when running, pool swim, cardio and the `other` category each have a fetched summary and file. Otherwise `unknown`.
5. Manual, multisport and type keys: `confirmed` when the manual and multisport categories were both sampled. `not_observed` when the account has neither. `unknown` when only one was sampled.
6. Rate limits and missing files: `confirmed` if any rate-limit header or 429 was seen. Otherwise `not_observed`, with a note that the absence of headers does not prove there are no limits.

**Exit codes and stdout:**
- `0`: outcome `complete` and leak check passed.
- `2`: outcome `partial`, `stopped` or `needs_token_field`.
- `1`: unexpected error or any leak-check failure.
- `3`: API key not found (no report written).

Every run that gets past credential loading writes `contract-report.json` and `.md`, even when it stops early. stdout prints the outcome, the report path, the fixture count, and any action needed (for example "rerun with --token-field"). Progress goes to stderr.

#### B7. Sanitization (`src/garmin_archive/devtools/sanitize.py`)

`sanitize(value, ctx) -> value` is a pure function over parsed JSON. `ctx` carries the per-run pseudonym map and the collected originals. The pseudonym salt/map is random per run and never written to disk. Rules:

1. **Catalog sub-objects are kept verbatim.** The entire contents of the objects under the keys `activityType`, `activityTypeDTO`, `eventType`, `eventTypeDTO`, `timeZoneUnitDTO`, `deviceDisplayUnitsDTO`, `unitOfPoolLength` and `fileFormat`, wherever they appear.
2. **Identifiers.** These are replaced with consistent pseudonyms (the same original always gives the same pseudonym within a run, so parent/child links survive):
   - values of the keys `activityId`, `parentId`, `childIds` (each element), `userProfileId`, `ownerId`, `deviceId`, `unitId`, `serialNumber`, `activityUUID` and `uuid`
   - values of any other key ending in `Id`, `ID` or `UUID` when the value is an integer with ≥ 6 digits, a string of ≥ 6 digits, or a UUID string

   Pseudonym formats:
   - integers: a random integer with the same digit count and no leading zero
   - digit strings: a random digit string of the same length
   - UUID strings: a random UUID4
   - any other string under an identifier key: a random alphanumeric string of the same length

   Every pseudonym is unique within the run and never equals any original value seen in the run.
3. **Free text and names.** String values of keys matching (case-insensitive regex) `(name|fullname|displayname|description|comment|email|locationname)$` become `"REDACTED"`. This includes the elements of string arrays under such keys, for example `activityLikeDisplayNames`. Rule 1 takes precedence, so names inside catalog objects are kept.
4. **URLs.** Any string beginning with `http://` or `https://`, under any key, becomes `"https://redacted.invalid/"`.
5. **Coordinates.** Numeric values of keys containing `latitude` or `longitude` (case-insensitive) become `null`.
6. **Everything else passes through unchanged.** This covers timestamps, numeric metrics, units and unknown keys.
7. **Leak check.** This step is mandatory.
   - It runs on **every** file written under `--fixtures-dir`, including the report, the manifests and the redaction log.
   - Sensitive originals are: replaced strings of ≥ 4 characters and replaced identifiers of ≥ 5 digits (as decimal strings), excluding any value that also occurs unchanged as a pass-through value somewhere in the same source document (for example a name that equals a `typeKey`).
   - For the report, manifests and redaction log, the sensitive set is the union of all originals collected in the run, and the pass-through exclusion uses pass-through values from all source documents.
   - Matching is a case-sensitive exact substring search over the serialized file, except that numeric originals match only when no digit is immediately before or after them.
   - On a hit: do not write that file, record `{file, json_path}` (never the value) in `leak_check.failed_fixtures`, continue the run, and exit `1`.

## Files to Modify

| File | Change |
|---|---|
| `pyproject.toml` | New: packaging, dependencies, dev group, ruff/pyright/pytest config |
| `uv.lock` | New: generated lockfile |
| `src/garmin_archive/__init__.py` | New: `__version__` |
| `src/garmin_archive/cli.py` | New: Typer app with `--version` |
| `src/garmin_archive/devtools/__init__.py` | New |
| `src/garmin_archive/devtools/sanitize.py` | New: sanitization and leak check |
| `src/garmin_archive/devtools/discovery/` (`__init__.py`, `__main__.py`, `credentials.py`, `http.py`, `capture.py`, `fileinfo.py`, `report.py`) | New: discovery tool (the module split is a suggestion) |
| `tests/conftest.py` | New: `live` marker gating, env and cwd isolation |
| `tests/devtools/...` | New: unit tests for all of the above |
| `tests/fixtures/contract/.gitkeep` | New: placeholder. The real sanitized fixtures come later from a human-run capture. |

`docs/upstream/garmin-personal-api.openapi.json` and `docs/upstream/manifest.json` already exist (the OpenAPI snapshot from SPEC §13 step 1). Do not modify them.

## Acceptance Criteria

1. `uv sync` succeeds. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` all pass with zero errors.
2. `uv run garmin-archive --version` prints the package version and exits 0. `uv run garmin-archive --help` exits 0.
3. A test marked `live` is skipped when `GARMIN_LIVE_TESTS` is unset. The autouse fixture removes both key env vars and runs each test in `tmp_path`.
4. Credentials:
   - `GARMIN_API_KEY` takes precedence over `GARMIN_CONNECT_API_KEY`.
   - The `--env-file` and default `./.env` fallback works with quotes and comments.
   - A missing key gives a clear error naming both variables.
   - The argument parser has no option that accepts the key.
5. End-to-end capture with `httpx.MockTransport` against a fake API. The fake API serves:
   - a JSON token response `{"accessToken": "...", "expiresIn": 3600}`
   - 3+ search pages ending with an empty page
   - 100 items for the `limit=200` and `limit=1000` probes
   - one duplicate ID across pages
   - a multisport parent with two children
   - a manual activity whose file download returns 404
   - a pool swim
   - one 429 with `Retry-After: 2`

   The test asserts, at minimum, that `contract-report.json` has:
   - `outcome == "complete"`
   - `token.token_field == "accessToken"`, `token.token_field_source == "auto"` and `token.expiry_fields.expiresIn == 3600`
   - `page_size_cap.probes[0].returned == 100`
   - `pagination.duplicate_id_count == 1`
   - two `multisport-1-child-<k>` samples
   - a manual sample with `original_file.http_status == 404`
   - `http.status_429_count == 1`
   - the injected sleep called with 2

   It also asserts that the fixture files listed in B6 exist for each sample.
6. Across every file written in that test run (raw directory and fixtures directory) and all captured stdout/stderr, neither the fake API key nor the fake token string appears. The test also checks that no raw fake activity ID or fake profile ID appears anywhere under the fixtures directory.
7. Token handling:
   - Ambiguous bodies such as `{"token": "...", "idToken": "..."}` give outcome `needs_token_field` and exit code 2.
   - `--token-field token` then succeeds.
   - A 401 on the Bearer probe gives outcome `stopped` with `header_convention.probe_http_status == 401`.
   - A non-2xx token response stops with only its status recorded.
8. Pagination:
   - Reaching `--max-pages` gives `inventory_status == "incomplete"`, outcome `partial` and exit code 2, and sampling still runs.
   - The ordering probe reports `stable == false` when the fake reorders the first page.
9. HTTP:
   - A 302 to another host is not followed. The mock asserts that no request reaches that host and that the `Authorization` header is never sent there.
   - With `--delay 0`, a persistent 500 on a summary results in exactly 4 requests for that resource, with sleeps of 1, 2 and 4, a recorded failure, and the run continuing.
   - A `Retry-After: 300` stops the run with outcome `partial`.
10. File analysis:
    - A ZIP with a `..` member, more than 64 entries, or an oversized declared uncompressed size is recorded as a finding without raising and without writing any file.
    - A corrupt `.fit` member records a decode error.
    - A minimal valid FIT file built in memory by a test helper (file_id plus one record message, correct header and CRC) yields the expected message-type counts.
    - ZIP member names have 6+ digit runs pseudonymized.
11. Sanitization:
    - A parent's `childIds` equal its children's pseudonymized `activityId`s.
    - Integer digit count is preserved, and pseudonyms never equal originals.
    - Names, descriptions, `activityLikeDisplayNames` elements and URLs are redacted, and coordinates are nulled.
    - The contents of `activityType`, `timeZoneUnitDTO` etc., `typeKey`, `typeId` and timestamps are unchanged.
    - The leak check skips writing a file that contains a planted original value, records it in `leak_check.failed_fixtures`, and the run exits 1.
    - A redacted name equal to a pass-through `typeKey` does not trigger the leak check.
12. The test suite never reads the repository's real `.env` and never contacts the real Garmin API. The conftest isolation from AC3 enforces this, and the HTTP tests use only `MockTransport`.

## Pending Decisions

- **Resolved:** The env var name for the API key is `GARMIN_API_KEY` (from SPEC §4), with `GARMIN_CONNECT_API_KEY` accepted as a fallback because the developer's `.env` uses that name. The production credential spec decides the final behavior.
- **Resolved:** The OpenAPI snapshot is taken manually and committed in `docs/upstream/`. The discovery tool does not scrape the swagger page.
- **Resolved:** Activity timestamps are kept in sanitized fixtures because DST and UTC/local processing tests need them. Names, IDs, coordinates and URLs are redacted.
- **Out of scope:** the SQLite archive, the production `auth.py`/`api.py`, sync, processing and query. Running the capture against the real account is a manual step after merge, and committing its sanitized output is a separate, human-reviewed change.
