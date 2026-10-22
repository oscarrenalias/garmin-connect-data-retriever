---
name: "Storage foundation: SQLite connections, migrations and archive schema"
id: spec-ab0fae6c
description: "SQLite connections, atomic checksummed migrations, SPEC §5.2 schema, account binding, writer lock, ArchiveClient.initialize and the init command with the JSON envelope"
dependencies: null
priority: high
complexity: null
status: done
tags:
- storage
- migrations
- cli
scope:
  in: null
  out: null
feature_root_id: B-e4b32239
---
# Storage foundation: SQLite connections, migrations and archive schema

## Objective

Implement the `garmin_archive` storage foundation:
- typed errors
- database path resolution
- SQLite connection management
- an atomic, numbered, checksummed migration runner
- the archive and operational schema from `docs/SPEC.md` §5.2
- account binding
- a single-writer lock
- `ArchiveClient.initialize()`
- the `garmin-archive init` command with the SPEC §9 JSON envelope

The raw-archive write and read primitives come in the follow-up spec "Raw archive writer".

## Problems to Fix

1. Nothing can persist yet. Later specs (raw archive writer, sync, query) all need a database with a known schema and safe connections.
2. SPEC §12 requires explicit numbered transactional migrations and refusal of newer schema versions. Query commands must never implicitly create or migrate a missing database. Without one migration runner and an explicit "open existing" path, every later spec would invent its own.
3. SPEC §4 requires that the database is bound to one Garmin `userProfileId` and rejects data from another profile.
4. SPEC §6.2 requires a single writer per database.
5. SPEC §9 defines a JSON output envelope and exit codes. The first real command should establish the shared helper so later commands reuse it.

## Changes

All modules live under `src/garmin_archive/`. Library code follows CLAUDE.md: no printing, no `sys.exit`, an injectable clock.

### 1. Errors and shared models (`models.py`)

- Base class `GarminArchiveError(Exception)`. Attributes:
  - `code: str` (stable, upper snake case)
  - `message: str`
  - `retryable: bool = False`
  - `details: dict[str, object]` (JSON-serializable, never containing secrets or Garmin IDs)
- Subclasses:

| Class | `code` | Raised when |
|---|---|---|
| `StorageError` | `STORAGE_ERROR` | Generic SQLite or IO failure. It wraps `sqlite3.Error`/`OSError`, keeping the original as `__cause__`. |
| `DatabaseNotFoundError(StorageError)` | `DATABASE_NOT_FOUND` | An "open existing" operation targets a path that does not exist. |
| `NotInitializedError(StorageError)` | `NOT_INITIALIZED` | The file exists but has no `schema_migrations` table, or is behind the packaged version, on a path that must not migrate. |
| `SchemaVersionError(StorageError)` | `SCHEMA_TOO_NEW` | The DB records a version higher than this package knows. |
| `MigrationChecksumError(StorageError)` | `MIGRATION_CHECKSUM_MISMATCH` | An applied migration's recorded checksum differs from the packaged one. |
| `ArchiveLockedError(StorageError)` | `ARCHIVE_LOCKED`, `retryable=True` | Another writer holds the lock. |
| `AccountMismatchError(GarminArchiveError)` | `ACCOUNT_MISMATCH` | A profile ID differs from the bound one. |

- A `Clock` protocol with `now() -> datetime`. It must return an aware UTC datetime. Provide a `SystemClock` implementation.
- `utc_rfc3339(dt: datetime) -> str` formats as `YYYY-MM-DDTHH:MM:SS.ffffffZ`. A naive datetime raises `ValueError`. Every timestamp written by the storage layer uses this with the injected clock.

### 2. Configuration and database path (`config.py`)

- `ArchiveConfig` is a frozen dataclass with `db_path: Path | None = None` and `busy_timeout_ms: int = 5000`.
- `resolve_db_path(config: ArchiveConfig, environ: Mapping[str, str]) -> Path` uses the first available of:
  1. `config.db_path`
  2. `environ["GARMIN_ARCHIVE_DB"]` if it is set and non-empty
  3. `Path(platformdirs.user_data_dir("garmin-archive", appauthor=False)) / "archive.db"`

  The result is always `expanduser().resolve()`d, so it is absolute.
- Add `platformdirs` as a runtime dependency. Do not hard-code platform paths.
- Config-file loading is out of scope.

### 3. Connections (`storage/connection.py`)

- `open_write_connection(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection`:
  - `isolation_level=None`, so transactions are explicit.
  - `PRAGMA foreign_keys=ON`.
  - `PRAGMA journal_mode=WAL`. Verify the returned mode is `wal`; otherwise raise `StorageError`.
  - `PRAGMA busy_timeout=<ms>` and `PRAGMA synchronous=NORMAL`.
- `open_read_connection(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection`:
  - Raise `DatabaseNotFoundError` if the file does not exist. It must never create a file.
  - Open the URI `file:<percent-encoded absolute path>?mode=ro` with `uri=True`. **Never** use `immutable=1` (SPEC §10).
  - `PRAGMA query_only=ON`, `foreign_keys=ON` and the busy timeout.
- Both `open_*_connection` functions wrap any `sqlite3.Error`/`OSError` raised while connecting or setting pragmas in `StorageError` (e.g. a non-SQLite file). Errors from statements executed later by callers are not wrapped. The read URI is built by a pure helper `_read_only_uri(path) -> str` so tests can inspect it.
- `transaction(conn)` is a context manager that issues `BEGIN IMMEDIATE`, then `COMMIT` on success or `ROLLBACK` on any exception, which is re-raised. It is used by later specs. Migrations use the approach in §4 instead.
- File permissions: before first opening a **new** database file, `initialize` creates parent directories (`parents=True, exist_ok=True`). On POSIX, the new DB file is created with mode `0o600` (create it empty with `os.open(..., O_CREAT|O_EXCL, 0o600)` before connecting). After migrations, `chmod 0o600` any existing `-wal` and `-shm` files. On Windows (`sys.platform == "win32"`), skip all chmod calls.

### 4. Migrations (`storage/migrations.py`, `storage/migrations/NNNN_<slug>.sql`)

- `Migration` is a frozen dataclass: `version: int`, `name: str`, `sql: str`, `checksum: str`, where `checksum` is the SHA-256 hex of the UTF-8 file bytes.
- `MigrationSource` is a protocol: `def load(self) -> Sequence[Migration]`, sorted by version, with unique versions.
  - `PackagedMigrations` loads `storage/migrations/*.sql` via `importlib.resources`. The version is the integer prefix of `NNNN_<slug>.sql`.
  - `DirectoryMigrations(path)` loads from a filesystem directory. It is used by tests.
  - Both reject duplicate or non-numeric prefixes with `ValueError`.
- Ensure hatchling includes `storage/migrations/*.sql` in the wheel. A test asserts that `PackagedMigrations().load()` returns version 1.
- `schema_migrations(version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL, checksum TEXT NOT NULL)` is created by the runner with `CREATE TABLE IF NOT EXISTS` before reading versions.
- `apply_migrations(conn, clock, *, source: MigrationSource | None = None) -> list[int]`. `source` defaults to `PackagedMigrations()`.
  1. Read the applied versions.
  2. Call `verify(conn, migrations)`: if any applied version exceeds the highest available version, raise `SchemaVersionError` with `details={"db_version": n, "supported_version": m}`. If any applied version's checksum differs, raise `MigrationChecksumError` with `details={"version": n}`.
  3. Apply each pending migration in ascending order, **atomically**. Python's `executescript` commits any open transaction first, so do not wrap it in `transaction()`. Instead, execute one script via `conn.executescript(...)` with this content:

     ```
     BEGIN IMMEDIATE;
     <migration sql>
     ;
     INSERT INTO schema_migrations(version, applied_at_utc, checksum) VALUES (<v>, '<ts>', '<checksum>');
     COMMIT;
     ```

     The values are ints and hex strings generated by the runner, never user input. If it raises, execute `ROLLBACK` if `conn.in_transaction`, then raise `StorageError` (`details={"version": v}`). Earlier migrations stay applied.
  4. Return the versions applied in this call.
- `check_schema(conn, *, source: MigrationSource | None = None) -> int` is for paths that must not migrate.
  - If there is no `schema_migrations` table, raise `NotInitializedError`.
  - Run `verify` (too new, checksums).
  - If the DB is behind the highest available version, raise `NotInitializedError` with `details={"db_version": n, "supported_version": m}`.
  - Otherwise return the version.
- `current_schema_version(conn) -> int` is the maximum applied version, or 0 if the table is absent or empty.
- Migration SQL files contain only DDL. The runner rejects a file with `ValueError` at load time if, after `--` line comments are removed, it matches `\b(BEGIN|COMMIT|ROLLBACK)\b` case-insensitively. `check_schema` must not create tables, because it runs on read-only connections; it treats a missing `schema_migrations` table as uninitialized.

### 5. Schema `0001_archive.sql` (SPEC §5.2)

Create exactly these **8** tables. Conventions:
- TEXT for identifiers, statuses and timestamps. Garmin IDs are TEXT. Locally generated IDs are `INTEGER PRIMARY KEY`.
- `*_at_utc` columns are RFC 3339 UTC strings ending in `Z`.
- Every `*_json` column has `CHECK(<col> IS NULL OR json_valid(<col>))`. `archive_metadata.value_json` is also `NOT NULL`.

Tables:

- `archive_metadata(key TEXT PRIMARY KEY, value_json TEXT NOT NULL)`
- `sync_runs`
  - `id INTEGER PRIMARY KEY`
  - `mode TEXT NOT NULL CHECK(mode IN ('full','incremental','scoped','activity','resume','retry_failed'))`
  - `started_at_utc TEXT NOT NULL`, `finished_at_utc TEXT`
  - `status TEXT NOT NULL CHECK(status IN ('running','complete','partial','failed','interrupted'))`
  - `parameters_json TEXT`, `counters_json TEXT`, `coverage_json TEXT`
- `raw_payloads`
  - `id INTEGER PRIMARY KEY`
  - `resource_key TEXT NOT NULL`, `activity_id TEXT`
  - `kind TEXT NOT NULL CHECK(kind IN ('search_page','summary','workouts','original_file','imported_file'))`
  - `content_type TEXT`, `encoding TEXT`
  - `body_text TEXT`, `body_blob BLOB NOT NULL`
  - `sha256 TEXT NOT NULL`, `byte_count INTEGER NOT NULL`
  - `first_seen_at_utc TEXT NOT NULL`
  - `UNIQUE(resource_key, sha256)`
- `fetch_events`
  - `id INTEGER PRIMARY KEY`
  - `sync_run_id INTEGER REFERENCES sync_runs(id)`
  - `resource_key TEXT NOT NULL`, `method TEXT NOT NULL`, `path TEXT NOT NULL`
  - `request_params_json TEXT`
  - `fetched_at_utc TEXT NOT NULL`
  - `http_status INTEGER`
  - `payload_id INTEGER REFERENCES raw_payloads(id)`
  - `etag TEXT`, `last_modified TEXT`, `error_code TEXT`
- `activity_archive`
  - `activity_id TEXT PRIMARY KEY`
  - `owner_profile_id TEXT`
  - `parent_activity_id TEXT`, deliberately with **no FK** (SPEC §7.2)
  - `type_key TEXT`
  - `first_seen_at_utc TEXT NOT NULL`, `last_seen_at_utc TEXT NOT NULL`
  - `last_summary_check_at_utc TEXT`
  - `availability_status TEXT NOT NULL DEFAULT 'listed' CHECK(availability_status IN ('listed','not_found','forbidden'))`
- `activity_resources`
  - `activity_id TEXT NOT NULL REFERENCES activity_archive(activity_id)`
  - `kind TEXT NOT NULL CHECK(kind IN ('search_item','summary','workouts','original_file','imported_file'))`
  - `current_payload_id INTEGER REFERENCES raw_payloads(id)`
  - `json_pointer TEXT`
  - `state TEXT NOT NULL CHECK(state IN ('pending','available','unavailable','forbidden','failed'))`
  - `last_attempt_at_utc TEXT`, `last_success_at_utc TEXT`, `next_retry_at_utc TEXT`
  - `error_code TEXT`
  - `PRIMARY KEY(activity_id, kind)`
- `sync_checkpoints`
  - `scope TEXT PRIMARY KEY`
  - `window_start TEXT`, `window_end TEXT`
  - `next_offset INTEGER`
  - `last_completed_at_utc TEXT`
  - `run_id INTEGER REFERENCES sync_runs(id)`
- `processing_runs`
  - `id INTEGER PRIMARY KEY`
  - `activity_id TEXT NOT NULL REFERENCES activity_archive(activity_id)`
  - `processor_version TEXT NOT NULL`, `input_fingerprint TEXT NOT NULL`
  - `status TEXT NOT NULL CHECK(status IN ('pending','processed','partial','unsupported','failed'))`
  - `started_at_utc TEXT NOT NULL`, `finished_at_utc TEXT`
  - `warnings_json TEXT`, `error_code TEXT`

Indexes:
- `raw_payloads(activity_id, kind)`
- `fetch_events(resource_key, fetched_at_utc)`
- `fetch_events(sync_run_id)`
- `activity_archive(parent_activity_id)`
- `activity_archive(type_key)`
- `activity_resources(state)`
- `processing_runs(activity_id, started_at_utc)`

`search_item` resource rows represent the SPEC §5.1 link from an activity to its search-page item. `current_payload_id` references a `search_page` payload and `json_pointer` is the item's array position, for example `/17`. `availability_status` `not_found` records a summary 404 (SPEC §5.2) and never implies deletion. The live contract run observed no ETag or Last-Modified headers (`tests/fixtures/contract/contract-report.md`), so those columns are nullable.

### 6. Metadata and account binding (`storage/metadata.py`)

- `get_metadata(conn, key) -> object | None` and `set_metadata(conn, key, value)` handle JSON (de)serialization. `set_metadata` performs an upsert and does not open its own transaction. Callers decide the transaction boundary.
- `get_account_binding(conn) -> str | None` reads `archive_metadata['account_binding']`, stored as `{"user_profile_id": "<TEXT>", "bound_at_utc": "<ts>"}`.
- `bind_account(conn, user_profile_id: str, clock) -> bool` runs inside its own `transaction(conn)`:
  - No binding yet: store it and return `True`.
  - Same ID: return `False` and write nothing.
  - Different ID: raise `AccountMismatchError`. Neither the message nor `details` contains either ID, and the DB is unchanged.
  - Empty `user_profile_id`: raise `ValueError`.
- Later sync code calls this with the `userProfileId` from a verified activity summary. This spec provides and tests the primitive only.

### 7. Writer lock (`storage/lock.py`)

- `writer_lock(db_path: Path)` is a context manager taking an exclusive, non-blocking OS lock on `<db_path>.lock`:
  - POSIX: `fcntl.flock(fd, LOCK_EX | LOCK_NB)`.
  - Windows: `msvcrt.locking(fd, LK_NBLCK, 1)`.
  - Create the lock file with mode `0o600` on POSIX.
- If the lock is already held, raise `ArchiveLockedError`.
- The lock is always released on exit, including when an exception is raised. The lock file may remain on disk.
- Guard platform imports with `sys.platform` checks so pyright passes on macOS and Linux.

### 8. Library entry point (`client.py`; re-export `ArchiveClient`, `ArchiveConfig`, `InitResult` and the error classes from `garmin_archive/__init__.py`)

- `ArchiveClient(config: ArchiveConfig | None = None, *, clock: Clock | None = None, environ: Mapping[str, str] | None = None, migrations: MigrationSource | None = None)`.
  - `environ` defaults to `os.environ`. `migrations` defaults to packaged migrations and is the test seam.
  - The client is a context manager. It holds no open connection between calls: every operation opens and closes its own connections. `close()` is idempotent and closes nothing in this spec, but must exist.
- `initialize() -> InitResult(db_path: Path, schema_version: int, created: bool, applied_migrations: list[int])`. In order, it:
  1. Resolves the path and records `created = not db_path.exists()`.
  2. Creates parent directories.
  3. Acquires `writer_lock(db_path)`.
  4. Creates the file securely if new (§3).
  5. Opens the write connection.
  6. Runs `apply_migrations`.
  7. Writes `archive_metadata['schema'] = {"schema_version": n, "package_version": __version__}` in a transaction.
  8. Closes the connection and releases the lock.

  It is idempotent: a second call returns `created=False, applied_migrations=[]`.
- `_open_existing_read() -> sqlite3.Connection` is internal. It resolves the path, raises `DatabaseNotFoundError` if the file is missing, opens a read connection and runs `check_schema`. It never creates or migrates. Later query methods build on it.

### 9. CLI output helper and `init` (`output.py`, `cli.py`)

`output.py`:
- `success(data: Mapping[str, object], *, warnings: list[str] | None = None) -> dict` returns `{"status": "ok", "data": …, "meta": {"output_version": 1}}`. It adds `"warnings"` only when the list is non-empty.
- `error(code, message, *, retryable=False, details=None) -> dict` returns `{"status": "error", "error": {"code", "message", "retryable", "details"}}`.
- `exit_code_for(exc: BaseException) -> int` maps:

| Exception | Exit code |
|---|---|
| `AccountMismatchError` | 3 |
| any `StorageError` (including subclasses) | 5 |
| any other `GarminArchiveError` | 1 |
| usage errors | 2 |
| unexpected exceptions | 1 |

- `emit(document, stream)` writes one JSON document (`ensure_ascii=False`, trailing newline) to the given stream. The CLI passes `sys.stdout`.

`cli.py`:
- Add a global `--db PATH` option on the callback, stored in the Typer context object. When omitted, `GARMIN_ARCHIVE_DB` and the platform default apply through `resolve_db_path`.
- `garmin-archive init` calls `ArchiveClient(ArchiveConfig(db_path=...)).initialize()`. On success it emits `success({"db_path": str(abs path), "schema_version": n, "created": bool, "applied_migrations": [...]})` and exits 0.
- Error handling:
  - Entry point: `main(argv: Sequence[str] | None = None) -> int` invokes the Typer app with `standalone_mode=False` and returns the exit code. `_console_entry()` calls `sys.exit(main())` and is the console-script target. Rename the existing Typer callback (currently `main`) to `_root`. The existing `--version`/`--help` tests keep using `CliRunner` on `app`.
  - `click.UsageError` and its subclasses become `error("INVALID_ARGUMENT", <click message>)` with exit 2.
  - `GarminArchiveError` becomes `error(e.code, e.message, retryable=e.retryable, details=e.details)` with exit `exit_code_for(e)`.
  - Any other exception becomes `error("INTERNAL_ERROR", "unexpected error")` with exit 1. The traceback goes to stderr only.
  - `--help` and `--version` keep their normal plain-text behaviour and exit 0.
- Update `[project.scripts]` to `garmin-archive = "garmin_archive.cli:_console_entry"`.
- Move `__version__` into `src/garmin_archive/_version.py`. Both `__init__.py` and `client.py` import it from there, which avoids a circular import.
- Exit code 1 is used for internal and unclassified errors. SPEC §9 does not define code 1, so this is recorded as a resolved decision below.
- The CLI contains no SQL, path, lock or migration logic.

## Files to Modify

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | Add `platformdirs`; include `storage/migrations/*.sql` in the wheel; script entry `garmin_archive.cli:_console_entry` |
| `src/garmin_archive/models.py` | New: errors, `Clock`, `SystemClock`, `utc_rfc3339` |
| `src/garmin_archive/config.py` | New |
| `src/garmin_archive/storage/__init__.py`, `connection.py`, `migrations.py`, `migrations/0001_archive.sql`, `metadata.py`, `lock.py` | New |
| `src/garmin_archive/client.py` | New |
| `src/garmin_archive/__init__.py` | Re-exports |
| `src/garmin_archive/output.py` | New |
| `src/garmin_archive/cli.py` | `main()`, `--db`, `init` |
| `tests/storage/...`, `tests/test_client.py`, `tests/test_cli_init.py`, `tests/test_output.py` | New tests |

Do not modify `src/garmin_archive/devtools/**` or `tests/fixtures/**`.

## Acceptance Criteria

1. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` all pass with zero errors. The existing CLI `--version`/`--help` tests still pass.
2. `resolve_db_path`: an explicit path wins over `GARMIN_ARCHIVE_DB`, which wins over the default. The default equals the monkeypatched `platformdirs.user_data_dir(...)/archive.db`. A relative or `~` path comes back absolute.
3. After `initialize()` on a new path in `tmp_path`:
   - The tables are exactly `archive_metadata`, `sync_runs`, `raw_payloads`, `fetch_events`, `activity_archive`, `activity_resources`, `sync_checkpoints`, `processing_runs` and `schema_migrations`, compared as a set from `sqlite_master`.
   - All 7 indexes listed exist.
   - `current_schema_version == 1`.
   - `PRAGMA journal_mode` is `wal` and `PRAGMA foreign_keys` is `1` on a write connection.
   - On POSIX, the DB file mode is `0o600`.
   - `archive_metadata['schema']` has `schema_version` 1.
   - A second `initialize()` returns `created=False` and `applied_migrations=[]`.
4. Constraints: inserting a `raw_payloads` row with kind `bogus`, an `activity_resources` row with state `bogus`, invalid JSON into `sync_runs.parameters_json`, or an `activity_resources` row for a non-existent activity each raises `sqlite3.IntegrityError`.
5. Migrations, using `DirectoryMigrations` in `tmp_path`:
   - A DB with an applied version 99 raises `SchemaVersionError`, with `details["db_version"] == 99`.
   - Changing the recorded checksum of version 1 raises `MigrationChecksumError`.
   - A migration 2 containing a valid `CREATE TABLE t2` followed by an invalid statement leaves version 1 applied, no `t2` table, and no `schema_migrations` row for 2, and raises `StorageError`.
   - A migration file containing `COMMIT` is rejected at load time.
   - `PackagedMigrations().load()` returns exactly version 1.
6. `check_schema` raises `NotInitializedError` on an empty SQLite file and on a DB behind the source's highest version. `_open_existing_read()` on a missing path raises `DatabaseNotFoundError` and creates no file.
7. Read connection: an `INSERT` via `open_read_connection` raises `sqlite3.OperationalError`. A test asserts the opened URI contains `mode=ro` and not `immutable`.
8. Account binding:
   - The first bind returns `True` and stores the ID as TEXT.
   - The same ID returns `False`.
   - A different ID raises `AccountMismatchError`, with neither ID in `str(exc)`, `exc.message` or `exc.details`, and the stored binding unchanged.
   - An empty ID raises `ValueError`.
9. Writer lock: while one `writer_lock` is held, acquiring it from a child process (`multiprocessing`, spawn context) raises `ArchiveLockedError` in the child. After release it succeeds. `initialize()` while the lock is held raises `ArchiveLockedError`.
10. CLI. Tests call `main([...])` directly, capture stdout with `capsys`, and assert the returned exit code. Each case writes exactly one JSON document to stdout:
    - `garmin-archive --db <tmp>/a.db init` succeeds with exit 0, and the data has `created: true` and `schema_version: 1`.
    - Running it again gives `created: false`.
    - A DB with an injected applied version 99 gives exit 5 and code `SCHEMA_TOO_NEW`.
    - An unknown option such as `init --bogus` gives exit 2 and code `INVALID_ARGUMENT`.
    - A held writer lock gives exit 5 and code `ARCHIVE_LOCKED`, with `retryable: true`.
    - `--db` pointing at a non-SQLite file (random bytes) gives exit 5 and code `STORAGE_ERROR`.
11. `output.exit_code_for` maps every row of the §9 table, unit-tested.
12. With a fixed clock, `schema_migrations.applied_at_utc` and the account binding's `bound_at_utc` equal the expected exact strings ending in `Z`. `utc_rfc3339` rejects naive datetimes.

## Pending Decisions

- **Resolved:** `search_item` is an `activity_resources.kind`, representing SPEC §5.1's JSON-pointer link from an activity to its search-page item.
- **Resolved:** `availability_status` values are `listed | not_found | forbidden`.
- **Resolved:** Exit code 1 is used for internal or unclassified errors (`INTERNAL_ERROR`). SPEC §9 lists no meaning for 1.
- **Resolved:** SPEC §3 lists `archive.py` at the package top level for raw payload persistence. That module is created by the follow-up "Raw archive writer" spec, at `src/garmin_archive/archive.py`, as SPEC §3 places it.
- **Out of scope:** raw-archive writer/reader (next spec), sync, HTTP adapter, credentials, query/introspection, export/validate/backup, config files, analytical tables.
