# Garmin personal activity archive — specification

Status: implementation specification, v0.1  
Reviewed: 2026-10-22  
Proposed package: `garmin_archive`  
Proposed executable: `garmin-archive`

## 1. Purpose and scope

Build a local-first Python library and a thin CLI that preserve the user's Garmin Connect activities and enable ad-hoc SQL analysis by a future agent skill.

Required outcomes:

- Discover and archive **all activity types**, including unsupported types and multisport parents and children.
- Preserve original activity API responses without flattening their contents into domain columns.
- Preserve original downloadable activity files inside the same SQLite database.
- Produce a rebuildable analytical model for running, swimming and cardio, including available sessions, laps, lengths and time-series samples.
- Expose schema discovery, descriptions, samples and read-only SQL with deterministic JSON output.
- Operate without cloud storage, an LLM, a web server or Garmin credentials for local analysis.

One database represents one Garmin account. Multiple users, Garmin write operations, dashboards, coaching recommendations, scheduled daemons, and daily wellness/health ingestion are outside v1. “All activities” means recorded activities available through activity search, not every Garmin account resource (sleep, nutrition, goals, etc.). The separate wellness activity endpoint is outside this scope.

The existing receipt-processing project's CLI is the interface reference [4]. Reuse its `schema`, `describe`, `sample` and `sql` concepts and tabular JSON shape, but allow safe analytical CTEs and window functions.

## 2. Verified upstream contract and outstanding checks

Sources [1–3] are authoritative for Garmin behavior. All other choices below are application requirements, not claims about Garmin guarantees.

### 2.1 Authentication

The Personal API uses a user-created API key to obtain an access token. No browser OAuth consent callback, client secret or multi-user OAuth flow is required for this application.

```http
POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token
Authorization: ApiKey <personal-api-key>
```

Tokens last up to 24 hours, capped by key expiry. There are no refresh tokens; request another token with the key. Deleting the key also invalidates associated tokens. Key lifetime is selected in Connect (30/90/180 days or one year). [1,2]

Authenticated data calls shall use `Authorization: Bearer <access-token>`. Verify this header convention and the exact token response field names with the first live integration: the inspected Swagger document lacks security scheme definitions and does not include the token endpoint response schema. Do not invent fields such as `access_token` or `expires_in` in implementation without confirming them.

### 2.2 Activity endpoints

Base URL: `https://personal.connectapi.garmin.com`.

| Method and path | Purpose | Documented response |
| --- | --- | --- |
| GET `/activitylist-service/activities/search/activities` | Paginated activity discovery | JSON array of `SummarizedActivityDTO` |
| GET `/activity-service/activity/{activityId}` | Detailed summary, metadata, measurements and split summaries | `ActivitySummaryDTO` JSON |
| GET `/activity-service/activity/{activityId}/workouts` | Workouts associated with an activity | JSON array; empty array is valid |
| GET `/download-service/files/activity/{activityId}` | Original activity file | `application/x-zip-compressed` |

Activity search supports `start` (zero-based offset; default 0), `limit` (default 99), `sortOrder`, `sortBy`, `startDate` and `endDate`. Date filters are inclusive and refer to the activity's **local date**. `excludeChildren` defaults to true: the client MUST explicitly send `excludeChildren=false`. Omit sport, subtype, favorite, required-field and other narrowing filters for full discovery. Use the default local-start ordering explicitly where supported (`sortBy=startLocal`). [3]

The publicly embedded OpenAPI definition was inspected, rather than relying only on the Swagger page's text shell. It declares OpenAPI 3.0.1 and API version 1.0.0. It exposes no separate activity sample/lap/length endpoint in the inspected activity services. Obtain fine-grained records from original FIT files; do not silently use private Garmin web endpoints.

### 2.3 Implementation discovery gate

Before implementing the HTTP adapter, record a sanitized contract fixture covering:

1. Token body field names, header convention and available expiry metadata.
2. First and final search pages, effective page-size limit and ordering stability.
3. Oldest accessible activity and whether any historical restrictions exist.
4. Detailed summaries and ZIP contents for running, cardio, pool swim and one other sport.
5. Manual activities without original files, multisport parent/child file behavior, and supported type keys.
6. Any documented rate limits/headers and response behavior for missing files.

The inspected docs do not establish unlimited history, numeric rate limits, snapshot pagination, deletion feeds or a last-modified cursor. Report these as unknown until confirmed. “All activities archived” must be qualified by accessible history and per-resource coverage; never infer completeness solely from a successful HTTP response.

## 3. Architecture and runtime

Use Python 3.11+, standard `sqlite3` with JSON functions, `httpx`, the official `garmin-fit-sdk`, and a CLI framework such as Typer. Package with `pyproject.toml`, a `src/` layout and `uv` development tooling. Support Linux/ARM64 (including Raspberry Pi), macOS and Windows; do not hard-code platform paths.

```text
src/garmin_archive/
  auth.py          # credential provider and token lifecycle
  api.py           # documented Garmin HTTP adapter
  archive.py       # raw payload persistence and export
  sync.py          # discovery, work queue, retries, coverage
  processing/      # common, FIT, running, swimming, cardio
  storage/         # connections, migrations, repositories
  query.py         # introspection and read-only SQL
  models.py        # typed public inputs/results/errors
  cli.py           # argument parsing and output only
```

All behavior must be callable through the library. CLI code must not contain authentication, SQL authorization, ingestion or sport mapping logic. No library method prints, exits the process or prompts; dependencies for HTTP, credentials, clock and progress reporting are injectable.

## 4. Credentials and authorization

Credential lookup: the `GARMIN_API_KEY` (or `GARMIN_CONNECT_API_KEY`) environment variable, then the same variable names in a `.env` file (the current directory's `.env`, or `--env-file PATH`). The application never stores the key: there is no OS keyring integration, no key file written by the application, and no `auth configure` command. The key is never accepted as a command-line argument. *(Decision 2026-10-22: the user rejected keyring and persisted-credential storage.)*

- Never persist keys, tokens, token endpoint responses or Authorization headers in the activity database, logs, fixtures or CLI output.
- Acquire a fresh access token on each CLI invocation and hold it in memory only for that process's lifetime. Tokens are **never persisted** anywhere. Within a process, bind the cached token to the credential identity and invalidate it on rotation.
- Hold one token per process, with no expiry tracking. Tokens last 24 h, which covers any run. On a data-call 401, renew once.
- On data-request 401, obtain a fresh token and retry once. A second 401 stops with `AUTH_FAILED`. Do not renew indefinitely.
- A 403 is an authorization/resource failure, not a token-refresh loop. Surface it with the affected resource.
- Network calls have bounded connect/read timeouts. Never forward credentials to a different host through redirects.
- The local CLI is single-user and relies on OS file permissions; it does not implement an additional login system. Protect the database, backups and credentials appropriately.
- Bind the database to `userProfileId` from a verified activity summary. Reject subsequent ingestion from another profile. An empty account remains unverified until identity can be established; never infer identity from the key string.

`auth status` reports whether a key is configured and from which source, without secrets. `auth check` performs token acquisition and a minimal read. There is no `auth clear`, because nothing is cached between invocations. Key revocation is performed in Garmin Connect.

## 5. Archival storage

### 5.1 Preservation rules

Keep received JSON/text unchanged in a TEXT field, not parsed and re-serialized. Preserve the response entity bytes as received after HTTP transfer/content decoding in a BLOB as the authoritative byte copy; transport compression need not be preserved. Store ZIP/FIT or other binary responses directly as BLOBs, not base64 text. TEXT is null when decoding is not appropriate. This permits JSON SQL inspection while retaining exact payload hashes and binary fidelity.

Capture whole search pages, detailed summaries, workout responses and original file downloads. Every activity links back to its search-page item using a JSON pointer. Authentication responses are explicitly excluded. Store only an allowlist of response metadata (content type, ETag, Last-Modified); never persist cookies or credentials.

Changed resources create new immutable payload versions. Identical bodies for the same resource reuse the payload row, with a new fetch observation. Never delete archived records merely because a resource disappears upstream. Raw responses survive processing failures and schema changes.

### 5.2 Archive and operational tables

Unless noted, identifiers/statuses/dates are TEXT; IDs generated locally are INTEGER primary keys. Garmin IDs are stored and emitted as TEXT to avoid downstream JSON integer precision loss. Quantities use REAL and counts use INTEGER.

| Table | Required columns and constraints |
| --- | --- |
| `archive_metadata` | `key` PK, `value_json`; account binding, schema version, contract fingerprint |
| `raw_payloads` | `id` PK, `resource_key`, `activity_id` nullable, `kind`, `content_type`, `encoding`, `body_text` nullable, `body_blob` NOT NULL, `sha256`, `byte_count`, `first_seen_at_utc`; UNIQUE(resource_key, sha256) |
| `fetch_events` | `id` PK, `sync_run_id`, `resource_key`, `method`, `path`, sanitized `request_params_json`, `fetched_at_utc`, `http_status`, `payload_id` nullable FK, `etag`, `last_modified`, `error_code` nullable |
| `activity_archive` | `activity_id` PK, `owner_profile_id`, `parent_activity_id` nullable, `type_key`, `first_seen_at_utc`, `last_seen_at_utc`, `last_summary_check_at_utc`, `availability_status` |
| `activity_resources` | `activity_id` FK, `kind`, `current_payload_id` FK nullable, `json_pointer` nullable, `state`, `last_attempt_at_utc`, `last_success_at_utc`, `next_retry_at_utc`, `error_code`; PK(activity_id, kind) |
| `sync_runs` | `id` PK, `mode`, `started_at_utc`, `finished_at_utc`, `status`, `parameters_json`, `counters_json`, `coverage_json` |
| `sync_checkpoints` | `scope` PK, `window_start`, `window_end`, `next_offset`, `last_completed_at_utc`, `run_id`; discovery progress, not proof of resource completion |
| `processing_runs` | `id` PK, `activity_id`, `processor_version`, `input_fingerprint`, `status`, `started_at_utc`, `finished_at_utc`, `warnings_json`, `error_code` |
| `schema_migrations` | `version` PK, `applied_at_utc`, `checksum` |

Resource kinds: `search_page`, `summary`, `workouts`, `original_file`, and `imported_file`. Resource states: `pending`, `available`, `unavailable`, `forbidden`, `failed`. Processing states: `pending`, `processed`, `partial`, `unsupported`, `failed`.

A 404 on a file does not mean the activity was deleted. A summary 404 records `not_found`, not a destructive deletion. Error observations belong in `fetch_events`; retaining error bodies is unnecessary and must not expose credentials.

## 6. Synchronization

### 6.1 Initial and full synchronization

`update --full` enumerates every accessible activity without a sport filter. Fetch pages of 99 initially; adjust only to verified server constraints. Request ascending ordering, include children and deduplicate by activity ID. Continue until an empty page; do not treat a short page as conclusive if the server may cap page size.

For every discovered activity, enqueue summary, workouts and original-file retrieval. Fetch originals for **every sport**, regardless of whether a processor exists. Persist each response before attempting normalization. Empty workout arrays count as complete resources. Missing originals must appear in coverage reporting, without preventing summary processing.

Pagination is not an atomic snapshot: deduplicate overlapping pages, repeat boundary pages on resume, detect repeated pages/no-progress, and reconcile the discovered inventory after a full pass. Mark inventory coverage provisional when concurrent changes prevent stabilization. A hard page ceiling or repeated-page failure is an incomplete run, never success.

### 6.2 Incremental synchronization

`update` on an empty database performs full discovery. Otherwise, v1 scans the full paginated summary inventory on each invocation to discover older backdated uploads as well as new activities. This favors correctness over minimizing small list requests.

- Fetch detail resources for new activities and resources still missing or failed.
- Refresh summary/workouts/originals for activities whose list item changed and activities in a configurable recent window (default 14 days).
- Compare per-item canonical JSON fingerprints for change detection, while preserving original page text. Do not hash the whole page to decide whether every activity changed.
- Older detail-only edits may not appear in list summaries. `update --full` re-fetches all resources to reconcile these; report the last full refresh timestamp.
- Explicit `--from`/`--to` dates limit discovery using Garmin's inclusive local-date semantics and mark the run as scoped, not an all-history refresh.
- `--activity-id` updates one activity. `--resume <run-id>` resumes the durable queue and rechecks pagination boundaries. `--retry-failed` retries outstanding failed resources without rediscovering everything.

Use a single writer lock for a database and bounded HTTP concurrency (default 1). Never hold a SQLite write transaction during a network request. Commit payload, fetch event and resource pointer atomically; advance checkpoints only after associated discovery work is durable.

For 429, honor Retry-After (seconds or date) and bounded backoff with jitter. For network errors and retryable 5xx, use at most five attempts per request. An excessive wait leaves resumable work and reports partial completion. Do not retry malformed 400 requests unchanged. Separate permanent unavailable resources from transient failures.

Counters must include discovered, new, changed, unchanged, downloaded, unavailable, forbidden, failed and processed activities/resources. Report archived summaries and original-file coverage separately. Network/archive success and analytical processing success are independent outcomes.

## 7. Analytical data model

### 7.1 Conventions

All tables below are derived and rebuildable. Every normalized record links to its processing run and source payload. Use nullable metrics; absence is not zero. UTC timestamps are RFC 3339 strings ending in Z; also retain local timestamp, activity-local date and source timezone information. Never interpret Garmin's offset-free `startTimeGMT` using the host timezone.

Canonical units: meters, seconds, meters/second, bpm, steps/minute, strokes/minute, watts, kcal. Use explicit suffixes. Retain unknown type keys and source enums. The processor version covers classification, conversion and formula changes.

Common summaries cover every activity, when fields are available. Detailed sport processors cover running and its verified subtypes; pool and open-water swimming; and cardio/HIIT/stair activity types verified against real fixtures. A versioned type registry chooses the processor. Do not classify every non-running sport as cardio or infer sport from an activity's name.

### 7.2 Tables and grain

| Table | Grain and keys | Columns beyond keys/provenance |
| --- | --- | --- |
| `activities` | One row per Garmin activity; `activity_id` PK | `parent_activity_id`, `is_multisport_parent`, `type_key`, `sport_family`, `name`, `description`, `start_at_utc`, `start_local`, `local_date`, `timezone`, `distance_m`, `timer_duration_s`, `elapsed_duration_s`, `moving_duration_s`, `avg_speed_mps`, `max_speed_mps`, `avg_hr_bpm`, `max_hr_bpm`, `calories_kcal`, `elevation_gain_m`, `elevation_loss_m`, `aerobic_training_effect`, `anaerobic_training_effect`, `training_load`, `processing_status`, `processed_at_utc` |
| `activity_sessions` | One row per FIT session; PK(activity_id, session_index) | `fit_sport`, `fit_subsport`, `start_at_utc`, `distance_m`, timer/elapsed duration, average/max HR, source FIT message index |
| `activity_laps` | One actual lap per session; PK(activity_id, session_index, lap_index) | `start_at_utc`, `distance_m`, timer/elapsed duration, average/max HR, average speed, cadence and its unit, `lap_trigger`, `intensity` |
| `activity_samples` | One source record; PK(activity_id, session_index, sample_index) | `timestamp_utc`, `elapsed_s`, `distance_m`, `speed_mps`, `hr_bpm`, `cadence`, `cadence_unit`, `altitude_m`, `latitude_deg`, `longitude_deg`, `power_w`, `temperature_c` |
| `running_metrics` | One row per running activity; activity_id PK/FK | `avg_cadence_spm`, `max_cadence_spm`, `avg_stride_length_m`, `avg_ground_contact_ms`, `avg_vertical_oscillation_cm`, `avg_vertical_ratio_pct`, `avg_power_w`, `max_power_w` |
| `swimming_metrics` | One row per swim; activity_id PK/FK | `swim_environment` (pool/open_water/unknown), `pool_length_m`, `active_lengths`, `total_strokes`, `avg_swolf`, `avg_distance_per_stroke_m`, `avg_stroke_rate_spm`, `active_swim_duration_s`, `recorded_rest_duration_s`, `length_coverage_status` |
| `swim_lengths` | One FIT length, including idle lengths; PK(activity_id, session_index, length_index) | nullable `lap_index`, `start_at_utc`, `length_type`, `stroke_type`, `distance_m`, `timer_duration_s`, `elapsed_duration_s`, `stroke_count`, `avg_stroke_rate_spm`, `swolf` |
| `cardio_metrics` | One row per cardio activity; activity_id PK/FK | `cardio_subtype`, `steps`, `floors_climbed`, `floors_descended`, `total_reps`, `total_sets` |
| `activity_intervals` | Source-declared interval; PK(activity_id, session_index, interval_index) | `kind`, `start_at_utc`, `end_at_utc`, `duration_s`, `distance_m`, `avg_hr_bpm`, `max_hr_bpm`, `source_kind` |
| `metric_provenance` | One populated analytical metric | table name, serialized row key, column name, source payload ID, JSON pointer/FIT message reference, conversion/formula identifier |

Every derived row includes `processing_run_id` and `source_payload_id`. Measurements with a different source use `metric_provenance`. In v1, summary metrics use detail JSON first, then list JSON, then FIT fallback; samples/laps/lengths use FIT. A partial latest resource refresh must be visible as mixed freshness. Do not silently claim all inputs are from one snapshot.

Parent IDs may reference an activity not yet downloaded: preserve that ID without a mandatory parent FK. All other child tables reference their owning activity. Use composite FKs for session/lap relationships where present. Index activity date/family, parent ID, sample timestamps, resource keys and processing status.

### 7.3 Mapping and processing rules

For detail JSON, use `activityId`, `activityName`, `activityTypeDTO`, `parentId`, `isMultiSportParent`, `timeZoneUnitDTO` and `summaryDTO`. Initial direct mappings:

| JSON path under `summaryDTO` | Target | Conversion |
| --- | --- | --- |
| `startTimeGMT`, `startTimeLocal` | `start_at_utc`, `start_local` | Treat GMT as UTC; preserve local wall time |
| `distance`, `duration`, `elapsedDuration`, `movingDuration` | distance and corresponding duration columns | meters/seconds |
| `averageSpeed`, `maxSpeed` | speed columns | m/s |
| `averageHR`, `maxHR`, `calories` | HR and calorie columns | bpm/kcal |
| `averageRunCadence`, `maxRunCadence` | running cadence | already steps/minute; do not double |
| `strideLength` | `avg_stride_length_m` | centimeters divided by 100 |
| `groundContactTime`, `verticalOscillation` | running dynamics | milliseconds / centimeters respectively |
| `poolLength`, `unitOfPoolLength` | `pool_length_m` | Convert declared units, including yards; unknown unit => null plus warning |
| `numberOfActiveLengths`, `totalNumberOfStrokes`, `averageSWOLF`, `strokeDistance` | swim summary metrics | Retain reported semantics |
| `trainingEffect`, `anaerobicTrainingEffect`, `activityTrainingLoad` | common training metrics | Reported values, not locally reconstructed Garmin scores |

The complete implementation mapping registry must declare source paths, units, nullable behavior and fixture references for every populated column. Ambiguous fields remain null until their unit/meaning is verified. List DTO mappings are separate from detail DTO mappings.

Decode FIT using the official SDK. Preserve the original ZIP even if decoding fails; do not substitute decoded JSON for the original. Read ZIP members without unsafe path extraction and enforce configurable compressed/uncompressed size and entry-count limits. Support multiple FIT sessions and unknown messages/fields. Archive other formats unchanged; v1 may mark their detailed processing unsupported while retaining summary analytics.

- FIT cadence conventions can differ from Connect summary cadence. Convert to running steps/minute only with a verified source-specific rule; retain ambiguous cadence and its source unit.
- Do not equate JSON split summaries with chronological laps. Store laps from actual FIT lap messages; keep other splits raw until their grain is explicitly mapped.
- Preserve FIT records at native cadence; no invented one-second resampling, interpolation or zero-filled missing samples.
- Idle swim lengths are retained and excluded from swimming distance. Infer active length distance from pool length only when semantics are supported; drill lengths may lack stroke counts.
- Derive per-length SWOLF as timer seconds plus recorded strokes only when both are valid; label it derived. Preserve the source's stroke counting convention.
- Open-water swims have no inferred pool length or synthetic lengths. Keep pool and open-water analysis distinct.
- Pool active time and recorded rest use explicit length/event records. Do not label all elapsed-minus-timer time as swim rest. Set coverage partial when length records do not explain the session.
- Cardio intervals come from recorded laps/events/workout structure, not arbitrary heart-rate thresholds. Store unknown/missing intervals as absent, not inferred exercise bouts.
- Preserve multisport parents and children, but avoid summing both in default volume analysis. Do not parse the same parent FIT sessions into child activities without a verified correspondence.
- Changed inputs or processor versions trigger reprocessing. Replace an activity's derived rows atomically, removing obsolete rows. On failure retain its prior successful analytical version with an explicit stale/failed status. Reprocessing is offline and must not alter raw payloads.

### 7.4 Stable analytical views

Expose `v_activity_summary`, `v_running`, `v_swimming`, `v_cardio`, `v_swim_lengths` and `v_archive_coverage`.

`v_activity_summary` contains all activity summaries plus `include_in_volume_totals`, false for multisport parents and true otherwise. Consumers must apply this flag when aggregating volume; missing children must be visible in coverage. Sport views join common and sport-specific columns and exclude multisport parent duplication.

`v_running` exposes `timer_pace_s_per_km = timer_duration_s * 1000 / distance_m` for positive distance. `v_swimming` exposes both `timer_pace_s_per_100m` and `active_pace_s_per_100m`, the latter only with known active duration. `v_swim_lengths` exposes per-active-length pace for positive distance. Never average paces without weighting: aggregate time divided by aggregate distance. Document whether each metric includes rests/pauses.

Views and schema descriptions must state grain, joins, units, null semantics, freshness and parent/child counting rules. `v_archive_coverage` reports resource and processing states per activity.

## 8. Public library API

Proposed API (names are application design, not Garmin SDK methods):

```python
client = ArchiveClient(config, credential_provider=provider)
client.initialize()                       # schema and migrations
client.check_auth()                       # no token in result
client.update(SyncOptions(full=True))     # structured SyncResult
client.reprocess(ProcessOptions(all=True))
client.list_activities(ActivityFilter(...))
client.get_activity(activity_id)
client.schema()                          # live schema + semantic metadata
client.describe('v_running')
client.sample('v_running', limit=5)
client.query(sql, parameters={}, limit=5000, timeout_s=10)
client.export_raw(activity_id, destination)
client.status()
client.validate()
client.backup(destination)
```

Local methods must never access the network or require credentials. Network methods return explicit coverage and retry information. Define typed exceptions for authentication, authorization, rate limiting, transport, contract mismatch, storage, processing, invalid query and query timeout. Provide context management/close for resources and cancellation between work units.

## 9. CLI contract

Global options: `--db PATH`, `--config PATH`, `--output PATH`, `--version`, `--help`. CLI options override environment, which overrides config, which overrides defaults. Use `GARMIN_ARCHIVE_DB` for database location and platform-appropriate application directories otherwise.

JSON is the default and stable automation format. Human-readable formatting may be added to list/show/status, but `schema`, `describe`, `sample` and `sql` always emit JSON. Write progress/logs to stderr; stdout contains exactly one JSON result. `--output` writes the same result atomically. Credentials never appear in configuration inspection.

| Command | Purpose / principal options |
| --- | --- |
| `init` | Create database and apply known migrations |
| `auth check`, `auth status` | Credential validation and configuration status; no secret output |
| `update` | Incremental discovery/archive/process; `--full`, `--from`, `--to`, `--activity-id`, `--resume`, `--retry-failed`, `--archive-only` |
| `reprocess` | Offline rebuild; require `--all`, `--activity-id` or `--sport`; optional `--failed-only` |
| `list-activities` | Date/type/family/processing-state filters, deterministic order, `--limit`/`--offset` |
| `show-activity ID` | Summary, processor version, provenance and coverage; optional bounded `--include-laps` / `--include-lengths` |
| `schema` | Tables/views, fields, keys, relationships, schema version and descriptions |
| `describe NAME` | Grain, field types/units/nulls, join keys and examples for a table/view |
| `sample NAME --limit 5` | Small deterministic tabular preview; default excludes large text and BLOB payloads |
| `sql --query SQL` | Single read-only query; also support `--file PATH`, bound `--params JSON`, `--limit`, `--timeout` |
| `status` | Inventory totals, oldest/newest dates, last sync/full refresh, failures, raw/original/normalized coverage |
| `export-raw ID --output-dir PATH` | Exact archived payloads/files with manifest and SHA-256 hashes; `--all-versions` optional |
| `validate` | SQLite integrity/FK checks, payload hashes, stale processing and coverage inconsistencies |
| `backup --output PATH` | Consistent SQLite backup through the SQLite backup API, including WAL state |

`update` modifies the local archive only; it never edits Garmin activities. Reject incompatible selectors such as `--full` plus `--activity-id`. Binary file export is separate from JSON output; generic `--output` refers to the command result/manifest.

Tabular result shape follows the receipt CLI:

```json
{
  "status": "ok",
  "columns": ["month", "distance_km"],
  "rows": [["2026-10", 42.5]],
  "meta": {
    "row_count": 1,
    "truncated": false,
    "limit_applied": 5000,
    "execution_ms": 12,
    "schema_version": 1,
    "output_version": 1
  }
}
```

Non-tabular success returns `status`, `data`, `meta` and optional `warnings`. Errors return `status: error`, `error: {code, message, retryable, details}`. Partial sync returns `status: partial` with counters, failed work and resume information. Empty results are successful empty arrays.

Exit codes: 0 complete success; 2 invalid arguments/query; 3 authentication/authorization; 4 transport/rate limit failure; 5 database/migration failure; 6 partial synchronization/processing; 7 processing failure; 8 query timeout. Missing optional fields or source-confirmed unavailable original files generate coverage warnings, not automatic failure. Missing expected resources remain partial. Handle Ctrl-C with a resumable checkpoint and conventional exit 130.

## 10. Read-only SQL for agent analysis

Use a separate SQLite connection opened with `mode=ro`, `PRAGMA query_only=ON`, extension loading disabled and no custom functions that read files, run commands or access the network. Do not use `immutable=1` for a database concurrently updated in WAL mode.

Use SQLite's authorizer to deny writes, DDL, ATTACH/DETACH, transaction manipulation, arbitrary PRAGMA, extension loading and non-allowlisted functions. Allow reads only from explicitly registered analytical/archive tables and views. Schema introspection uses trusted library code rather than unrestricted user PRAGMAs. Apply the same protection through library and CLI.

Allow one SELECT, including read-only WITH/CTEs, joins, aggregates, subqueries, JSON functions and window functions. Whitespace/comments are acceptable; use SQLite preparation/authorizer checks, not keyword blacklists as the security boundary. Reject multiple statements. Bind parameters separately from SQL text; validate/quote identifiers for sample/describe through the live schema allowlist.

Defaults: 5,000 rows, maximum 100,000 by explicit option; ten-second execution budget; configurable output-byte budget, default 10 MiB. Fetch one extra row to determine truncation; limits apply even if the query omits LIMIT or supplies a larger one. Enforce time/VM-step limits using a progress handler. Output size and time bounds must also cover large individual values and expensive CTEs; SQL row limits alone are insufficient.

BLOB values are represented by type/byte-length metadata, never dumped into JSON. Raw JSON TEXT is queryable with SQLite JSON functions. Truncated results must say so; JSON must remain valid. Values remain data: activity titles/descriptions cannot change CLI execution or agent instructions.

The CLI does not require an arbitrary write-SQL escape hatch in v1. Local users can manage their own database externally; application migrations and ingestion own normal writes.

## 11. Agent usage examples

```bash
garmin-archive update
garmin-archive status
garmin-archive schema
garmin-archive describe v_running
garmin-archive sample v_swimming --limit 3
garmin-archive sql --query "SELECT substr(local_date,1,7) AS month, SUM(distance_m)/1000.0 AS distance_km FROM v_running GROUP BY month ORDER BY month"
garmin-archive sql --query "SELECT pool_length_m, SUM(distance_m) AS meters, 100.0*SUM(active_swim_duration_s)/SUM(distance_m) AS pace_s_per_100m FROM v_swimming WHERE swim_environment='pool' AND length_coverage_status='complete' AND active_swim_duration_s IS NOT NULL AND distance_m>0 GROUP BY pool_length_m"
garmin-archive sql --query "SELECT type_key, COUNT(*) AS activities FROM activities GROUP BY type_key ORDER BY activities DESC"
garmin-archive reprocess --sport swimming
```

The future skill should inspect schema/version, inspect data coverage, formulate SQL, and disclose missing/stale/truncated data. It should distinguish sport/pool lengths and avoid parent/child double counting. It should not need to know private Garmin endpoints or receive credentials. Weekly/monthly groupings use retained activity-local dates unless the caller explicitly requests another basis.

## 12. Reliability, migration and verification

Enable SQLite foreign keys, WAL and a bounded busy timeout. Use explicit numbered transactional migrations; back up before destructive derived-schema changes. Never modify raw bytes during migrations. Older clients must refuse unsupported newer schema versions. Query commands cannot implicitly create or migrate a missing database.

Required automated acceptance scenarios:

1. Token acquisition, expiry, early key expiry, rotation and exactly one 401 renewal; no secret in DB/output/logs.
2. More than two search pages, same-time activities, overlapping pages, server-capped pages, backdated upload and resume with concurrent insertion.
3. Unknown sport plus multisport children are archived without filtering or duplicate volume totals.
4. Second unchanged update creates no duplicate activities or payload versions; changed content preserves the old raw version.
5. ZIP bytes export with identical SHA-256; malformed/oversized archives cannot corrupt or escape storage.
6. A crash after archival but before processing is recoverable offline; processor-version rebuild replaces old derived rows atomically.
7. Run fixtures verify cadence and centimeter conversion; DST fixtures verify UTC/local grouping.
8. Pool fixtures cover 25 m, 50 m, yards, drills, mixed strokes and rests; open-water fixtures create no artificial pool lengths.
9. Cardio with no distance, missing HR, and explicit intervals remains usable without fabricated zeroes or intervals.
10. SQL handles CTE/window/JSON analysis and rejects writes, multiple statements, ATTACH, PRAGMA and extension/file functions; row/byte/time limits are tested.
11. Schema output matches migrations and view grain/units; sample/SQL JSON matches the published output contract.
12. Partial fetch failures do not advance a complete-resource checkpoint; unavailable originals and stale processing are visible in status.
13. Live backup restores to an independently queryable database with all archived BLOBs intact.

Use sanitized fixtures for normal tests. A small opt-in live integration suite validates the official API with locally supplied credentials and no account mutation. No unredacted activity files or credentials enter the source repository.

## 13. Delivery sequence and completion criteria

1. Confirm contract gaps with live fixtures; snapshot upstream OpenAPI with URL, date and hash in the implementation repository.
2. Implement library authentication, migrations, exact raw archiving, full discovery and durable synchronization.
3. Implement shared/FIT and running/swimming/cardio processors, provenance and analytical views.
4. Implement CLI discovery/query operations, guardrails, status/export/backup and agent-oriented help.
5. Verify end-to-end archive coverage against the account, offline reprocessing and the acceptance scenarios above.

V1 is complete when all accessible activity types are archived with explicit resource coverage; supported sports can be analyzed at the detail available in their source files; the library and CLI share all business logic; and an agent can discover schema and answer new SQL questions without Garmin credentials or bespoke analysis commands.

## References

[1] Garmin Personal API: https://developer.garmin.com/gc-developer-program/personal/personal-api/

[2] Garmin Personal API FAQ: https://developer.garmin.com/gc-developer-program/personal/personal-faq/

[3] Garmin Personal API Swagger: https://developer.garmin.com/gc-developer-program/personal/swagger-docs/ — embedded OpenAPI 3.0.1 / API 1.0.0 inspected on 2026-10-22. The public schema does not itself confirm account-specific availability or historical depth.

[4] Receipt processor interface: https://github.com/oscarrenalias/skill-grocery-receipt-processor — README inspected through https://raw.githubusercontent.com/oscarrenalias/skill-grocery-receipt-processor/main/README.md on 2026-10-22.

[5] Garmin FIT SDK: https://developer.garmin.com/fit/get-the-sdk/

[6] FIT activity decoding: https://developer.garmin.com/fit/articles/cookbook/decoding_activity_files.html
