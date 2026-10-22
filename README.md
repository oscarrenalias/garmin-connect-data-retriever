# garmin-archive

Local-first Python library and CLI (`garmin-archive`) that archives Garmin Connect activities
through the official Garmin Personal API into a SQLite database, with a read-only SQL interface.
The master specification is [docs/SPEC.md](docs/SPEC.md). Contributor rules are in
[CLAUDE.md](CLAUDE.md).

## Usage

### Creating the archive: `garmin-archive init`

```bash
garmin-archive init                      # default location
garmin-archive --db ./my-archive.db init # explicit path
GARMIN_ARCHIVE_DB=./my-archive.db garmin-archive init
```

`init` creates the SQLite database (and its parent directory) and applies all known schema
migrations. It is idempotent: running it again on an up-to-date database applies nothing. It
needs no credentials and makes no network calls.

### Archiving activities: `garmin-archive update`

```bash
garmin-archive update          # new, changed, recent and unfinished resources
garmin-archive update --full   # re-fetch every resource of every activity
```

`update` discovers every accessible activity (all sports) through the activity search and
archives each activity's summary, workouts and original file, byte-exact, in the database. It
needs an API key (see above) and a database created by `garmin-archive init`: it never creates
or migrates a database, and a missing one is a storage error (exit 5). It holds the writer lock
for the whole run (`ARCHIVE_LOCKED` if another writer has it).

By default a run fetches resources of activities that are new, whose search item changed, or
that started in the last 14 days, plus any resource still `pending` or `failed`. `--full`
fetches all three resources for every activity.

**Re-running.** There is no resume command. Pending work is stored durably, so after a
`partial` or `interrupted` run (or a crash) just run `update` again; run `update --full` again
to continue an interrupted full refresh. `unavailable` (404) and `forbidden` (403) resources
are known outcomes, not failures. Nothing already archived is ever deleted.

**Ctrl-C** stops the run cleanly between pages and resources. It is recorded as
`interrupted`, the JSON report is still written and the exit code is 130.

The top-level envelope `status` is the run status: `ok` for a complete run, `partial` (exit 6)
or `interrupted` (exit 130), or the error envelope for a failed run.

Output `data` fields:

| Field | Meaning |
| ----- | ------- |
| `run_id` | Id of the `sync_runs` row for this run |
| `status` | `complete`, `partial` or `interrupted` (a `failed` run is reported as an error instead) |
| `counters` | `discovered`, `new`, `changed`, `downloaded`, `unchanged`, `unavailable`, `forbidden`, `failed` |
| `coverage` | Per resource kind (`summary`, `workouts`, `original_file`), counts by state across the whole archive |
| `inventory` | `complete` if discovery reached the end of the search results, else `incomplete` |
| `incomplete_reason` | Why discovery stopped early, or `null` |
| `pending` | Number of resources still waiting to be fetched |
| `processed` | Activities whose derived rows were rebuilt at the end of the run (`processed` plus `partial` status) |
| `processing_failed` | Activities whose processing failed (see [Activity processing](#activity-processing)) |
| `hint` | `null` when complete; otherwise a prompt to run `update` (or `update --full`) again |

A run is `complete` only when discovery reached the end and nothing is `failed` or `pending`.

After the sync row is finalized, `update` processes every activity whose inputs changed (see
[Activity processing](#activity-processing)). Processing never changes the sync `status` or the
exit code: a processing failure only shows up in `processing_failed`, and is retried by the next
`update` or by `reprocess`.

### Rebuilding derived tables: `garmin-archive reprocess`

```bash
garmin-archive reprocess                       # rebuild every activity
garmin-archive reprocess --activity-id 1001    # rebuild one activity
```

`reprocess` rebuilds the derived tables from the archived payloads only. It needs no
credentials, makes no network calls and never changes raw payloads. It requires an existing
database (a missing one is a storage error, exit 5) and holds the writer lock
(`ARCHIVE_LOCKED` if another writer has it). It ignores input fingerprints, so it rebuilds
everything even when nothing changed, and keeps going after a per-activity failure.

Output `data`: `{"processed": n, "partial": n, "failed": n}`. `processed` counts only fully
processed activities; `partial` and `failed` are counted separately. Unlike `update`, the
`processed` count here excludes `partial`.

| Exit code | Meaning |
| --------- | ------- |
| 0 | No activity failed (partial results still exit 0) |
| 7 | At least one activity failed, including an unknown `--activity-id` (counted as one failure) |
| 5 | Storage failure: missing or uninitialized database, or `ARCHIVE_LOCKED` |

There is no `--sport` selector and no `--all` or `--failed-only` flag in v1: no selector means
all activities.

### Activity processing

Processing turns archived raw payloads into rebuildable derived tables. Every derived row
carries `processing_run_id` (the `processing_runs` row that built it) and `source_payload_id`
(the payload it came from). All metric columns are nullable: NULL means absent or unknown, never
0. Timestamps are UTC RFC 3339 strings ending in `Z`; offset-free Garmin timestamps are never
interpreted in the host timezone. Units are in the column suffix: `_m` meters, `_s` seconds,
`_mps` m/s, `_bpm`, `_w` watts, `_kcal`, `_kg`, `_c` Celsius, `_deg` degrees.

| Table | Grain and key | Source and notes |
| ----- | ------------- | ---------------- |
| `activities` | One row per activity; `activity_id` | Summary JSON first, activity-search item only for fields the summary lacks. Holds `type_key`, `sport_family`, names, UTC/local start, `local_date`, `timezone`, distance, durations, speed, HR, calories, elevation, training effect and load, plus `processing_status` and `processed_at_utc` |
| `activity_sessions` | One row per FIT session; `(activity_id, session_index)` | `fit_sport`, `fit_subsport`, start, distance, durations, HR, `fit_message_index` |
| `activity_laps` | One row per FIT lap message; `(activity_id, lap_index)` | `session_index` is a plain column, assigned by lap start time. JSON splits are not laps. `cadence` and `cadence_unit` exist but are not populated yet (always NULL) |
| `activity_samples` | One row per FIT record; `(activity_id, sample_index)` | Native rate: no resampling, interpolation or zero-filling. `elapsed_s` is measured from the first timestamped record. Position is converted from semicircles to degrees |
| `activity_sets` | One row per FIT set message; `(activity_id, set_index)` | `set_type`, start, `duration_s`, `repetitions`, `weight_kg`, `category` |

`cadence_unit` is `fit_rpm` whenever `cadence` is present and NULL otherwise: sample cadence is
the raw FIT value, not converted to steps per minute, because FIT and Connect summary cadence
conventions differ.

A multisport parent keeps its own FIT sessions; they are never copied onto its children, and
parent ids may refer to activities not yet archived. A file that cannot be decoded as FIT (or is
missing) leaves the activity with only its `activities` row.

**Sport families.** `sport_family` comes from the Garmin `typeKey`, never from the activity
name. The mapping is versioned by `SPORTS_VERSION` (currently `1`):

| Family | `typeKey` values |
| ------ | ---------------- |
| `running` | `running`, `treadmill_running`, `trail_running`, `track_running`, `street_running`, `indoor_running`, `virtual_run` |
| `swimming` | `lap_swimming`, `open_water_swimming` |
| `hiit` | `hiit` |
| `cardio` | `indoor_cardio` |
| `cycling` | `cycling`, `road_biking`, `mountain_biking`, `gravel_cycling`, `indoor_cycling`, `virtual_ride` |
| `walking` | `walking`, `casual_walking`, `speed_walking` |
| `other` | any other or missing `typeKey` (the original `type_key` is kept) |

**Processing status.** Each attempt is a `processing_runs` row. `activities.processing_status`
is the status of the run that built the current rows:

- `processed`: all inputs were present and usable.
- `partial`: rows were built, but with warnings (missing or non-JSON summary, missing or
  undecodable original file, laps without a session). Warnings are stored in
  `processing_runs.warnings_json`.
- `failed`: the attempt raised an error. It is recorded only on the run (`status`, stable
  `error_code`, no message). The previous derived rows are kept untouched.

An activity's derived rows are replaced atomically in one transaction, so obsolete rows never
survive a rebuild.

**Fingerprint rules.** `update` processes an activity only when its input fingerprint differs
from the one on its latest `processed` or `partial` run. The fingerprint hashes the current
summary payload id, the current original-file payload id and the processor version
(`<package version>+sports<SPORTS_VERSION>`). The search item is deliberately excluded because
it is only a fallback source. Consequences: new or changed payloads and a new package or
`SPORTS_VERSION` trigger reprocessing; a `partial` run with unchanged inputs is not retried; a
`failed` run is retried, as it never counts as a successful run.

### API key and auth commands

Commands that call Garmin need a Personal API key. The key is read from, in order:

1. The `GARMIN_API_KEY` environment variable, then `GARMIN_CONNECT_API_KEY`.
2. The same variable names in a `.env` file: `./.env` by default, or the file given by the
   global `--env-file PATH` option (placed before the subcommand).

The environment wins over the `.env` file. The key is never accepted as a command-line
argument, and the application never stores the key or the access token: there is no keyring,
no key file and no `auth configure` command. A token is requested per invocation and kept in
memory only.

```bash
echo 'GARMIN_API_KEY=<your-key>' > .env       # or export GARMIN_API_KEY=<your-key>
garmin-archive auth status                    # is a key configured? (local, no network)
garmin-archive --env-file ~/secrets/garmin.env auth check
```

- `auth status` reports `{"configured": bool, "source": ...}`, where `source` is the variable
  name or the `.env` path, or `null`. It never prints the key, needs no network and always
  exits 0.
- `auth check` requests a token and runs a one-item activity search. On success it reports
  `{"authenticated": true, "search_status": 200}`. It makes no changes to the account or archive.
  Failures exit with code 3 (`MISSING_API_KEY`, `AUTH_FAILED`, `AUTHORIZATION_ERROR`) or
  code 4 (`TRANSPORT_ERROR`, `RATE_LIMITED`, `CONTRACT_ERROR`).

### Database path resolution

The database path is resolved in this order:

1. `--db PATH` (a global option, placed before the subcommand). The API key is never accepted
   as an argument.
2. The `GARMIN_ARCHIVE_DB` environment variable.
3. The platform user data directory, as `archive.db` (via `platformdirs`, app name
   `garmin-archive`; for example `~/Library/Application Support/garmin-archive/archive.db`
   on macOS and `~/.local/share/garmin-archive/archive.db` on Linux).

The path is expanded (`~`) and resolved to an absolute path.

### JSON output

Every command writes exactly one JSON document to stdout, followed by a newline.

Success:

```json
{
  "status": "ok",
  "data": {
    "db_path": "/abs/path/archive.db",
    "schema_version": 1,
    "created": true,
    "applied_migrations": [1]
  },
  "meta": {"output_version": 1}
}
```

`warnings` is added only when non-empty. Errors:

```json
{
  "status": "error",
  "error": {"code": "NOT_INITIALIZED", "message": "...", "retryable": false, "details": {}}
}
```

`details` is an empty object when there is nothing to add. Unexpected exceptions print a traceback to stderr and emit `INTERNAL_ERROR` on stdout.
Credentials and tokens never appear in output.

### Exit codes

Implemented so far (full table in [SPEC §9](docs/SPEC.md)):

| Code | Meaning |
| ---- | ------- |
| 0 | Success |
| 1 | Other library error, or `INTERNAL_ERROR` (unexpected exception) |
| 2 | Invalid arguments (`INVALID_ARGUMENT`) |
| 3 | Authentication or authorization failure (`MISSING_API_KEY`, `AUTH_FAILED`, `AUTHORIZATION_ERROR`), or `ACCOUNT_MISMATCH` (database is bound to a different Garmin account) |
| 4 | Transport, rate-limit or contract failure (`TRANSPORT_ERROR`, `RATE_LIMITED`, `CONTRACT_ERROR`) |
| 5 | Storage failure (`StorageError` and subclasses, such as `SCHEMA_TOO_NEW`, `MIGRATION_CHECKSUM_MISMATCH`, `ARCHIVE_LOCKED`, or a missing or uninitialized database) |
| 6 | `update` finished `partial`: some resources are `failed` or `pending`, or discovery was incomplete; run `update` again |
| 7 | `reprocess` finished with at least one failed activity |
| 130 | Interrupted (Ctrl-C) |

Code 8 is reserved for a later feature. `update` uses 0, 6 and 130 for run outcomes and 3, 4 and 5 for failures (4 also covers an API error before anything was archived).

### Storage notes

- Schema migrations are packaged SQL files, each recorded with a checksum; a database with a
  newer schema than the library knows is refused (`SCHEMA_TOO_NEW`).
- Writers take a single-writer lock on the archive (`ARCHIVE_LOCKED` if held).
- `activity_archive.availability_status` is one of `listed`, `not_found` or `forbidden`
  (default `listed`).
- Derived activity tables are created by migration 2 (`0002_activities.sql`); see
  [Activity processing](#activity-processing).
- `activity_resources.kind` is one of `search_item`, `summary`, `workouts`, `original_file`
  or `imported_file`. `search_item` represents the SPEC §5.1 link from an activity to its
  item inside a stored search-page payload (payload id plus JSON pointer).

### Raw archive writer and reader

`garmin_archive.archive` (re-exported from the package root as `ArchiveWriter`, `ArchiveReader`,
`RecordResult`, `RawPayload` and `FetchEvent`) stores Garmin responses exactly as received
(SPEC §5.1, §6.2). It is library-only and makes no network calls.

**Resource keys.** Every payload is identified by a resource key. Build keys with the
constructors rather than by hand:

| Constructor | Key |
| ----------- | --- |
| `search_page_key({"start": 0, "limit": 50})` | `search_page:limit=50:start=0` (params sorted by name; `start` and `limit` required; bools as `true`/`false`) |
| `summary_key("1001")` | `summary:1001` |
| `workouts_key("1001")` | `workouts:1001` |
| `original_file_key("1001")` | `original_file:1001` |

Activity ids are TEXT strings of ASCII digits. `validate_resource_key` rejects malformed keys
and keys whose `activity_id` disagrees. `imported_file` is reserved and cannot be recorded.

```python
writer = ArchiveWriter(conn, clock)  # conn: sqlite3 connection, clock: injected Clock
writer.upsert_activity(
    activity_id="1001", owner_profile_id=None, parent_activity_id=None, type_key=None
)
result = writer.record_response(
    resource_key=summary_key("1001"), kind="summary", activity_id="1001",
    sync_run_id=None, method="GET", path="/example/summary/1001", request_params=None,
    http_status=200, content_type="application/json", body=b'{"example": true}',
)
```

**`ArchiveWriter` methods.**

- `upsert_activity`: insert or update an `activity_archive` row; returns `True` when inserted.
- `mark_summary_check`: record the summary check time and availability status.
- `record_response`: store a body plus its fetch event and update `activity_resources`.
  Returns a `RecordResult` (`payload_id`, `fetch_event_id`, `is_new_payload`, `sha256`).
- `record_failure`: record a failed fetch (`unavailable`, `forbidden` or `failed`) with an
  error code and optional retry time; stores no body and returns the fetch event id.
- `record_search_item`: link an activity's `search_item` resource to an item
  (`json_pointer` of the form `/<index>`) inside a stored `search_page` payload.

**Error semantics.** Arguments are validated before anything is written, and bad input raises
`ValueError` (bad id, key, kind, state or availability status). Each method runs in exactly one
transaction; any `sqlite3.Error` is rolled back and re-raised as `StorageError` (original
exception chained as `__cause__`). Referencing an activity that is not archived, or a payload
that is not a `search_page`, also raises `StorageError`.

**Dedupe, versioning and retention.**

- Bodies are stored as received: a BLOB with the authoritative bytes, plus decoded TEXT only
  for JSON and `text/*` content types. JSON is never re-serialized.
- A payload is deduplicated on `(resource_key, sha256)`. Unchanged content reuses the existing
  row; changed content adds a new version. Every fetch appends a `fetch_events` row.
- The archive never deletes payloads. Failures and disappeared resources only add fetch events
  and change state; `current_payload_id` keeps pointing at the last good payload.
- `request_params` are stored as compact sorted JSON. Names containing `auth`, `token`, `key`,
  `secret`, `password` or `cookie` (case-insensitive) are rejected with `ValueError`, so
  credentials cannot be persisted through this path.

**`ArchiveReader`** is read-only (works on read-only connections): `get_payload` (raises
`KeyError` if absent), `current_payload(activity_id, kind)`, `payload_versions(resource_key)`,
`resource_states(activity_id)` and `fetch_events(resource_key)`. Read failures raise
`StorageError`.

## Development

```bash
uv sync                          # install dependencies
uv run pytest                    # tests
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run pyright                   # type check
uv run garmin-archive --help     # CLI
```

## Documentation

- [Contract discovery tool](docs/discovery.md): captures sanitized API contract fixtures. The
  API key is read from the environment or a dotenv file, never from a CLI argument. Raw
  captures stay in `.local/` and are never committed.
