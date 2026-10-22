"""Read-only SQL interface: queryable-object registry and live-schema introspection."""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from garmin_archive.models import InvalidQueryError, QueryTimeoutError
from garmin_archive.output import OUTPUT_VERSION, success
from garmin_archive.storage.migrations import current_schema_version


@dataclass(frozen=True)
class RegistryEntry:
    """A queryable object declared in code (name, description, grain)."""

    name: str
    description: str
    grain: str


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    pk: bool


@dataclass(frozen=True)
class QueryableObject:
    """A registry entry that exists in the live database, with live columns."""

    name: str
    kind: str  # "table" or "view"
    description: str
    grain: str
    columns: tuple[ColumnInfo, ...]


# Explicit allowlist; never derived from globbing at runtime.
REGISTRY: tuple[RegistryEntry, ...] = (
    RegistryEntry("activities", "Canonical activity summary rows.", "one row per activity"),
    RegistryEntry("activity_sessions", "FIT session records per activity.", "one row per session"),
    RegistryEntry("activity_laps", "FIT lap records per activity.", "one row per lap"),
    RegistryEntry("activity_samples", "FIT record samples per activity.", "one row per sample"),
    RegistryEntry(
        "activity_sets", "FIT set records (e.g. strength) per activity.", "one row per set"
    ),
    RegistryEntry("running_metrics", "Running-specific metrics.", "one row per running activity"),
    RegistryEntry(
        "swimming_metrics", "Swimming-specific metrics.", "one row per swimming activity"
    ),
    RegistryEntry("cycling_metrics", "Cycling-specific metrics.", "one row per cycling activity"),
    RegistryEntry("cardio_metrics", "Cardio-specific metrics.", "one row per cardio activity"),
    RegistryEntry(
        "swim_lengths", "Pool swim lengths from FIT length records.", "one row per length"
    ),
    RegistryEntry(
        "v_activity_summary",
        "All activities with include_in_volume_totals flag.",
        "one row per activity",
    ),
    RegistryEntry(
        "v_running",
        "Running activities joined with running metrics.",
        "one row per running activity",
    ),
    RegistryEntry(
        "v_swimming",
        "Swimming activities joined with swimming metrics.",
        "one row per swimming activity",
    ),
    RegistryEntry(
        "v_swim_lengths", "Swim lengths joined with activity context.", "one row per swim length"
    ),
    RegistryEntry(
        "v_cycling",
        "Cycling activities joined with cycling metrics.",
        "one row per cycling activity",
    ),
    RegistryEntry(
        "v_cardio", "Cardio activities joined with cardio metrics.", "one row per cardio activity"
    ),
    RegistryEntry(
        "raw_payloads", "Archived raw API response bodies.", "one row per payload version"
    ),
    RegistryEntry("activity_archive", "Archive state per Garmin activity.", "one row per activity"),
    RegistryEntry(
        "activity_resources",
        "Downloadable resources linked to activities.",
        "one row per activity resource",
    ),
    RegistryEntry("sync_runs", "History of sync runs.", "one row per sync run"),
)

_BY_NAME = {e.name: e for e in REGISTRY}


def quote_identifier(name: str) -> str:
    """Quote ``name`` as a SQLite identifier (double quotes, embedded quotes doubled)."""
    if "\x00" in name:
        raise InvalidQueryError("Invalid identifier")
    return '"' + name.replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, name: str) -> tuple[ColumnInfo, ...]:
    rows = conn.execute(f"PRAGMA table_info({quote_identifier(name)})").fetchall()
    return tuple(ColumnInfo(str(r[1]), str(r[2]), int(r[5]) > 0) for r in rows)


def list_queryable(conn: sqlite3.Connection) -> list[QueryableObject]:
    """Registry entries present in the live DB, in registry order.

    Must run in trusted library code, before any authorizer is installed.
    """
    kinds = {
        str(name): str(kind)
        for name, kind in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    result: list[QueryableObject] = []
    for entry in REGISTRY:
        kind = kinds.get(entry.name)
        if kind is None:
            continue
        result.append(
            QueryableObject(
                entry.name, kind, entry.description, entry.grain, _columns(conn, entry.name)
            )
        )
    return result


def lookup(conn: sqlite3.Connection, name: str) -> QueryableObject:
    """Return the live registry object ``name`` or raise InvalidQueryError."""
    if name in _BY_NAME:
        for obj in list_queryable(conn):
            if obj.name == name:
                return obj
    raise InvalidQueryError(f"Unknown table or view: {name!r}")


_ALLOWED_READ_TABLES = frozenset(_BY_NAME) | {"json_each", "json_tree"}
_DENIED_FUNCTIONS = frozenset({"load_extension"})


def authorizer(
    action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None
) -> int:
    """SQLite authorizer: permit only SELECT over registry objects and JSON helpers.

    Column, database and trigger/view/CTE-source arguments are ignored on purpose:
    the table name alone decides, so a CTE named like a registry view cannot
    smuggle in reads of other tables (the underlying READ is still checked).
    """
    if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_RECURSIVE:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        if arg1 in _ALLOWED_READ_TABLES:
            return sqlite3.SQLITE_OK
        # count(*) style reads carry no table, column or database.
        if not arg1 and not arg2 and db_name is None:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def harden_connection(conn: sqlite3.Connection) -> int:
    """Lock down a read connection for untrusted SQL; return the schema version.

    The schema version is read first, before the authorizer is installed.
    No custom SQL functions are registered.
    """
    version = current_schema_version(conn)
    enable = getattr(conn, "enable_load_extension", None)
    if enable is not None:
        with contextlib.suppress(sqlite3.Error, AttributeError, NotImplementedError):
            enable(False)
    conn.set_authorizer(authorizer)
    return version


def execute_guarded(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] | dict[str, object] = ()
) -> sqlite3.Cursor:
    """Execute ``sql`` on a hardened connection, mapping SQLite errors to InvalidQueryError."""
    try:
        return conn.execute(sql, params)
    except (sqlite3.DatabaseError, sqlite3.Warning, sqlite3.ProgrammingError) as exc:
        raise InvalidQueryError(str(exc)) from exc


DEFAULT_LIMIT = 5_000
MAX_LIMIT = 100_000
DEFAULT_TIMEOUT_S = 10.0
_PROGRESS_OPS = 1000


@dataclass(frozen=True)
class QueryResult:
    """Raw outcome of :func:`run_query` (rows are unconverted SQLite values)."""

    columns: list[str]
    rows: list[tuple[object, ...]]
    truncated: bool
    execution_ms: int


def run_query(
    conn: sqlite3.Connection,
    sql: str,
    parameters: object = None,
    limit: int = DEFAULT_LIMIT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> QueryResult:
    """Run one untrusted statement on a hardened connection with bound params.

    Fetches at most ``limit`` rows (reading ``limit + 1`` to detect truncation)
    and aborts via a progress handler once ``timeout_s`` has elapsed.
    """
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, Mapping):
        raise InvalidQueryError("Query parameters must be an object of named parameters")
    raw_params = cast("Mapping[object, object]", parameters)
    if not all(isinstance(k, str) for k in raw_params):
        raise InvalidQueryError("Query parameters must be an object of named parameters")
    params = {str(k): v for k, v in raw_params.items()}
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise InvalidQueryError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    if isinstance(timeout_s, bool) or not timeout_s > 0:
        raise InvalidQueryError("timeout must be a positive number of seconds")

    start = monotonic()
    deadline = start + timeout_s
    timed_out = False

    def on_progress() -> int:
        nonlocal timed_out
        if monotonic() >= deadline:
            timed_out = True
            return 1
        return 0

    conn.set_progress_handler(on_progress, _PROGRESS_OPS)
    try:
        cursor = conn.execute(sql, params)
        columns = [str(d[0]) for d in cursor.description or ()]
        fetched = cursor.fetchmany(limit + 1)
    except sqlite3.OperationalError as exc:
        if timed_out and "interrupt" in str(exc).lower():
            raise QueryTimeoutError(f"Query exceeded {timeout_s:g}s timeout") from exc
        raise InvalidQueryError(str(exc)) from exc
    except (sqlite3.Error, sqlite3.Warning) as exc:
        raise InvalidQueryError(str(exc)) from exc
    finally:
        conn.set_progress_handler(None, 0)

    truncated = len(fetched) > limit
    rows = [tuple(r) for r in fetched[:limit]]
    return QueryResult(columns, rows, truncated, int((monotonic() - start) * 1000))


DEFAULT_OUTPUT_BUDGET = 10 * 1024 * 1024


def convert_value(value: object) -> object:
    """Convert one SQLite value to a JSON-safe value (BLOBs become a size descriptor)."""
    if isinstance(value, bytes | bytearray):
        return {"type": "blob", "bytes": len(value)}
    if isinstance(value, memoryview):
        return {"type": "blob", "bytes": value.nbytes}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def tabular_result(
    result: QueryResult,
    *,
    limit_applied: int,
    schema_version: int,
    output_budget: int = DEFAULT_OUTPUT_BUDGET,
) -> dict[str, Any]:
    """Build the SPEC §9 tabular document, stopping before ``output_budget`` bytes.

    Row sizes are measured as serialized JSON (as emitted, ``ensure_ascii=False``)
    plus separators; once the next row would exceed the budget, output stops and
    ``truncated`` is set. If the first row alone is too large, ``rows`` is empty.
    """
    truncated = result.truncated
    rows: list[list[object]] = []
    used = len(json.dumps(result.columns, ensure_ascii=False))
    for raw in result.rows:
        row = [convert_value(v) for v in raw]
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        if used + size > output_budget:
            truncated = True
            break
        used += size
        rows.append(row)
    return {
        "status": "ok",
        "columns": result.columns,
        "rows": rows,
        "meta": {
            "row_count": len(rows),
            "truncated": truncated,
            "limit_applied": limit_applied,
            "execution_ms": result.execution_ms,
            "schema_version": schema_version,
            "output_version": OUTPUT_VERSION,
        },
    }


def _object_dict(obj: QueryableObject) -> dict[str, Any]:
    return {
        "name": obj.name,
        "kind": obj.kind,
        "description": obj.description,
        "grain": obj.grain,
        "columns": [{"name": c.name, "type": c.type, "pk": c.pk} for c in obj.columns],
    }


def schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """List every registry object present in the live DB (non-tabular payload).

    Must run on a trusted (not yet hardened) connection.
    """
    return success(
        {
            "schema_version": current_schema_version(conn),
            "objects": [_object_dict(o) for o in list_queryable(conn)],
        }
    )


def describe(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Describe one registry object, including its row count."""
    obj = lookup(conn, name)
    count = conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(obj.name)}").fetchone()[0]
    data = _object_dict(obj)
    data["row_count"] = int(count)
    data["schema_version"] = current_schema_version(conn)
    return success(data)


def _sample_order_by(obj: QueryableObject) -> str:
    pk = [c.name for c in obj.columns if c.pk]
    if pk:
        return ", ".join(quote_identifier(n) for n in pk)
    if obj.kind == "table":
        return "rowid"
    return quote_identifier(obj.columns[0].name) if obj.columns else "1"


def sample(
    conn: sqlite3.Connection,
    name: str,
    limit: int = 5,
    *,
    output_budget: int = DEFAULT_OUTPUT_BUDGET,
) -> dict[str, Any]:
    """Return the first ``limit`` rows of a registry object in deterministic order.

    Order: primary key columns, else ``rowid`` (tables), else the first column (views).
    """
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise InvalidQueryError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    obj = lookup(conn, name)
    sql = f"SELECT * FROM {quote_identifier(obj.name)} ORDER BY {_sample_order_by(obj)}"
    result = run_query(conn, sql, None, limit=limit)
    return tabular_result(
        result,
        limit_applied=limit,
        schema_version=current_schema_version(conn),
        output_budget=output_budget,
    )
