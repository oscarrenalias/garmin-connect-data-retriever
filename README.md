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
| `hint` | `null` when complete; otherwise a prompt to run `update` (or `update --full`) again |

A run is `complete` only when discovery reached the end and nothing is `failed` or `pending`.

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
| 130 | Interrupted (Ctrl-C) |

Codes 7 and 8 are reserved for later features. `update` uses 0, 6 and 130 for run outcomes and 3, 4 and 5 for failures (4 also covers an API error before anything was archived).

### Storage notes

- Schema migrations are packaged SQL files, each recorded with a checksum; a database with a
  newer schema than the library knows is refused (`SCHEMA_TOO_NEW`).
- Writers take a single-writer lock on the archive (`ARCHIVE_LOCKED` if held).
- `activity_archive.availability_status` is one of `listed`, `not_found` or `forbidden`
  (default `listed`).
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
