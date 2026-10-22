"""Sync run bookkeeping: ``sync_runs`` rows and final status derivation (SPEC 5.2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from garmin_archive.models import (
    AccountMismatchError,
    ApiAuthError,
    ApiTransportError,
    Clock,
    GarminArchiveError,
    utc_rfc3339,
)
from garmin_archive.storage.connection import transaction

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"


@dataclass
class SyncCounters:
    """Per-run counters recorded in ``sync_runs.counters_json``."""

    discovered: int = 0
    new: int = 0
    changed: int = 0
    downloaded: int = 0
    unchanged: int = 0
    unavailable: int = 0
    forbidden: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def start_run(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    full: bool = False,
    parameters: Mapping[str, object] | None = None,
) -> int:
    """Mark stale ``running`` rows ``interrupted``, then insert a new ``running`` row."""
    now = utc_rfc3339(clock.now())
    mode = MODE_FULL if full else MODE_INCREMENTAL
    parameters_json = json.dumps(dict(parameters or {}), sort_keys=True)
    with transaction(conn):
        conn.execute(
            "UPDATE sync_runs SET status = ?, finished_at_utc = ? WHERE status = ?",
            (STATUS_INTERRUPTED, now, STATUS_RUNNING),
        )
        cursor = conn.execute(
            "INSERT INTO sync_runs (mode, started_at_utc, status, parameters_json)"
            " VALUES (?, ?, ?, ?)",
            (mode, now, STATUS_RUNNING, parameters_json),
        )
        run_id = cursor.lastrowid
    if run_id is None:  # pragma: no cover - INSERT always sets lastrowid
        raise RuntimeError("sync_runs insert returned no row id")
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    clock: Clock,
    run_id: int,
    *,
    status: str,
    counters: SyncCounters,
    coverage: Mapping[str, object],
) -> None:
    """Record the final status, counters and coverage of a run."""
    with transaction(conn):
        conn.execute(
            "UPDATE sync_runs SET status = ?, finished_at_utc = ?,"
            " counters_json = ?, coverage_json = ? WHERE id = ?",
            (
                status,
                utc_rfc3339(clock.now()),
                json.dumps(counters.to_dict(), sort_keys=True),
                json.dumps(dict(coverage), sort_keys=True),
                run_id,
            ),
        )


def derive_status(
    *,
    reached_end: bool,
    counters: SyncCounters,
    pending: int,
    stop_requested: bool = False,
    error: GarminArchiveError | None = None,
    archived_anything: bool = False,
) -> str:
    """Derive the final run status.

    ``unavailable`` and ``forbidden`` are known outcomes; only ``failed`` counters and
    ``pending`` resources, or an unfinished discovery, make a run non-complete.
    """
    if stop_requested:
        return STATUS_INTERRUPTED
    if isinstance(error, (ApiAuthError, AccountMismatchError)):
        return STATUS_FAILED
    if isinstance(error, ApiTransportError) and not archived_anything:
        return STATUS_FAILED
    if error is None and reached_end and counters.failed == 0 and pending == 0:
        return STATUS_COMPLETE
    return STATUS_PARTIAL
