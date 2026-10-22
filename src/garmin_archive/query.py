"""Read-only SQL interface: queryable-object registry and live-schema introspection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from garmin_archive.models import InvalidQueryError


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
