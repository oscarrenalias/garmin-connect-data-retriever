---
name: "Query commands: schema, describe, sample, sql"
id: spec-6754bca2
description: null
dependencies: null
priority: null
complexity: null
status: planned
tags: []
scope:
  in: null
  out: null
feature_root_id: B-df83be1c
---
# Query commands: schema, describe, sample, sql

## Objective

Give an agent everything it needs to answer questions from the archive without Garmin credentials. That means four read-only commands: `schema`, `describe NAME`, `sample NAME` and `sql`, all emitting the deterministic JSON shape from `docs/SPEC.md` §9. The security rules are in SPEC §10 and are the important part of this spec.

## Context

- Single-user tool on a Raspberry Pi. Simple code, **no new dependencies**. Use only the stdlib `sqlite3`.
- Build on `storage.connection.open_read_connection`, which already opens `mode=ro` with `query_only=ON` and never creates a file.
- The queryable objects are:
  - the analytical tables and views from the processing specs (`activities`, `activity_*`, `*_metrics`, `swim_lengths`, `v_*`)
  - the archive tables `raw_payloads`, `activity_archive`, `activity_resources` and `sync_runs`, so raw JSON can be queried with `json_extract`
- Commands are local only: no network and no credentials. Query commands never create or migrate a database.

## Behaviour

1. **Registry (`query.py`).** A small in-code allowlist of queryable tables and views. Each entry has a one-line description and its grain, for example "one row per FIT length, including idle lengths".
   - Objects in the allowlist that don't exist in the live DB yet are left out. Every base table that a registry view reads from must itself be in the registry.
   - Columns, types and primary keys are read from the live schema by trusted library code. They are never entered by the user as a PRAGMA.
2. **Security boundary (`sql`).** This list is normative.
   - Open the connection with `open_read_connection`.
   - Call `enable_load_extension(False)` where available.
   - Register no custom functions.
   - Install `set_authorizer`. It allows only:
     - `SQLITE_SELECT`
     - `SQLITE_READ` **only when its table-name argument is in the registry, or is `json_each`/`json_tree`**. Match on the table name alone and ignore the column, database and view/trigger arguments. The 5th argument carries a user-chosen CTE name, so trusting it would let `WITH v_x AS (SELECT * FROM schema_migrations) …` through. `count(*)` reports an empty column and a `None` database, which must still pass.
     - `SQLITE_FUNCTION` for every function except `load_extension`. The stdlib `sqlite3` has no file or command functions unless extensions are loaded.
     - `SQLITE_RECURSIVE`, for CTEs
   - Everything else is denied: writes, DDL, `ATTACH`/`DETACH`, `PRAGMA` (including `pragma_*` table functions), transactions and `load_extension`.
   - Exactly one statement is allowed. Multiple statements are rejected; Python's `execute` refusing them is acceptable.
   - Parameters are bound from `--params JSON`, which must be an object of named parameters. They are never interpolated into the SQL.
   - Keyword blacklists are **not** the boundary.
   - `meta.schema_version` is read **before** the authorizer is installed, because the authorizer denies it afterwards.
3. **Limits.**
   - Default 5,000 rows, maximum 100,000 via `--limit`. Fetch one extra row to detect truncation. The limit applies even if the query has its own `LIMIT`.
   - Time budget: default 10 s, set with `--timeout`. Enforce it with `set_progress_handler`. The handler sets a `timed_out` flag when the deadline passes. Map `OperationalError('interrupted')` to `QUERY_TIMEOUT` only when that flag is set; anything else is `INVALID_QUERY`.
   - Output budget: 10 MiB of serialized JSON. Exceeding it truncates the rows and sets `truncated: true`. If the first row alone exceeds the budget, the result is `rows: []` with `truncated: true`.
   - The JSON must always remain valid.
4. **Values.**
   - BLOBs become `{"type": "blob", "bytes": n}` and are never dumped.
   - Text, numbers and NULL pass through unchanged.
   - Activity names and descriptions are data only.
5. **Output** (SPEC §9 tabular shape):
   ```json
   {"status":"ok","columns":[...],"rows":[[...]],"meta":{"row_count":n,"truncated":false,"limit_applied":5000,"execution_ms":12,"schema_version":3,"output_version":1}}
   ```
   - `schema` and `describe` use the non-tabular `{"status":"ok","data":...,"meta":...}`.
   - `schema` lists each exposed object: name, kind (table or view), description, grain, columns (name, type, pk), plus the schema version.
   - `describe NAME` returns one object with the same fields plus its `row_count`.
   - `sample NAME [--limit 5]` gives deterministic rows (ordered by primary key, or by `rowid` for tables, or by the first column for views), with BLOBs shown as metadata (the output budget applies).
   - The identifiers given to `describe` and `sample` are validated against the registry and quoted. Anything unknown is rejected.
6. **Errors.**
   - An invalid or denied query, or an unknown object, gives `INVALID_QUERY` and exit 2. The message comes from SQLite or the authorizer.
   - A timeout gives `QUERY_TIMEOUT` and exit 8.
   - A missing DB gives `DATABASE_NOT_FOUND` and exit 5, and creates no file.
   - `sql` takes `--query SQL`.
7. **Library API.** `ArchiveClient.schema()`, `describe(name)`, `sample(name, limit)` and `query(sql, parameters, limit, timeout_s)` return plain dicts. The CLI only serializes them.

## Acceptance Criteria

Tests run against a `tmp_path` DB created with `init`, with a few hand-inserted rows in `activities`, `raw_payloads` (with JSON `body_text` and a BLOB) and one view.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`.
2. A CTE with a window function, a join, `count(*)`, `json_extract(body_text, '$.summaryDTO.distance')`, `json_each(...)`, `printf`, `strftime` and a `SELECT` on a view all succeed.
3. Each of these is rejected with `INVALID_QUERY` and exit 2, and the DB is unchanged:
   - `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP`
   - `ATTACH DATABASE`, `PRAGMA table_info(x)`
   - `SELECT load_extension('x')`
   - `SELECT 1; SELECT 2`
   - a `SELECT` from `sqlite_master`
   - a `SELECT` from a non-allowlisted table such as `schema_migrations`
   - `BEGIN`
   - `WITH v_activity_summary AS (SELECT * FROM schema_migrations) SELECT * FROM v_activity_summary` (the CTE-name trick)
4. `--params '{"t": "running"}'` with `:t` binds correctly. A value containing `'; DROP TABLE activities; --` is treated as data.
5. Limits:
   - 6,000 matching rows with the default limit returns 5,000 rows with `truncated: true`. `--limit 100000` returns all 6,000.
   - `SELECT ... LIMIT 999999` still respects the limit.
   - A recursive CTE that runs forever stops with `QUERY_TIMEOUT` and exit 8 within about `--timeout 1` (generous margin).
   - A query returning a 20 MiB text value is truncated and still prints valid JSON.
6. A BLOB column appears as `{"type":"blob","bytes":n}`.
7. `schema`, `describe activities` and `sample v_activity_summary --limit 3` print one JSON document each in the documented shapes. `describe sqlite_master` is rejected.
8. Query commands on a missing DB give exit 5 and create no file. They never open a network connection, which is proven with a client whose HTTP transport fails on use.

## Out of scope

- write SQL
- query result caching
- human-readable table output
- `list-activities` / `show-activity`
