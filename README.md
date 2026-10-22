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
  "error": {"code": "NOT_INITIALIZED", "message": "...", "retryable": false, "details": null}
}
```

Unexpected exceptions print a traceback to stderr and emit `INTERNAL_ERROR` on stdout.
Credentials and tokens never appear in output.

### Exit codes

Implemented so far (full table in [SPEC §9](docs/SPEC.md)):

| Code | Meaning |
| ---- | ------- |
| 0 | Success |
| 1 | Other library error, or `INTERNAL_ERROR` (unexpected exception) |
| 2 | Invalid arguments (`INVALID_ARGUMENT`) |
| 3 | `ACCOUNT_MISMATCH` (database is bound to a different Garmin account) |
| 5 | Storage failure (`StorageError` and subclasses, such as `SCHEMA_TOO_NEW`, `MIGRATION_CHECKSUM_MISMATCH`, `ARCHIVE_LOCKED`) |
| 130 | Interrupted (Ctrl-C) |

Codes 4, 6, 7 and 8 are reserved for later features.

### Storage notes

- Schema migrations are packaged SQL files, each recorded with a checksum; a database with a
  newer schema than the library knows is refused (`SCHEMA_TOO_NEW`).
- Writers take a single-writer lock on the archive (`ARCHIVE_LOCKED` if held).
- `activity_archive.availability_status` is one of `listed`, `not_found` or `forbidden`
  (default `listed`).
- `activity_resources.kind` is one of `search_item`, `summary`, `workouts`, `original_file`
  or `imported_file`. `search_item` represents the SPEC §5.1 link from an activity to its
  item inside a stored search-page payload (payload id plus JSON pointer).

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
