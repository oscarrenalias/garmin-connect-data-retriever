"""Batch processing entry points (SPEC §7).

``process_pending`` handles only activities whose input fingerprint changed;
``reprocess`` rebuilds all activities or one, ignoring fingerprints, under the writer
lock. Both continue past per-activity failures and return counters.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from garmin_archive.models import Clock, GarminArchiveError, StorageError
from garmin_archive.processing.inputs import pending_activity_ids
from garmin_archive.processing.processor import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    process_activity,
)
from garmin_archive.storage.lock import writer_lock


@dataclass(frozen=True)
class BatchCounters:
    """Per-status counts for a batch run; ``processed`` excludes partial and failed."""

    processed: int = 0
    partial: int = 0
    failed: int = 0


def _run(conn: sqlite3.Connection, activity_ids: Iterable[str], clock: Clock) -> BatchCounters:
    processed = partial = failed = 0
    for activity_id in activity_ids:
        try:
            status = process_activity(conn, activity_id, clock).status
        except (GarminArchiveError, sqlite3.Error):
            status = STATUS_FAILED
        if status == STATUS_FAILED:
            failed += 1
        elif status == STATUS_PARTIAL:
            partial += 1
        else:
            processed += 1
    return BatchCounters(processed, partial, failed)


def process_pending(conn: sqlite3.Connection, clock: Clock) -> BatchCounters:
    """Process only activities whose inputs changed since their last successful run."""
    return _run(conn, pending_activity_ids(conn), clock)


def _db_path(conn: sqlite3.Connection) -> Path | None:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error as exc:
        raise StorageError(f"Could not determine database path: {exc}") from exc
    for row in rows:
        if row[1] == "main" and row[2]:
            return Path(str(row[2]))
    return None


def reprocess(
    conn: sqlite3.Connection, clock: Clock, activity_id: str | None = None
) -> BatchCounters:
    """Rebuild all activities, or only ``activity_id``, ignoring fingerprints.

    Holds the writer lock for on-disk databases. An unknown ``activity_id`` counts as one
    failure and records no run.
    """
    path = _db_path(conn)
    with writer_lock(path) if path is not None else nullcontext():
        try:
            known = [
                str(r[0])
                for r in conn.execute(
                    "SELECT activity_id FROM activity_archive ORDER BY activity_id"
                ).fetchall()
            ]
        except sqlite3.Error as exc:
            raise StorageError(f"Archive read failed: {exc}") from exc
        if activity_id is None:
            return _run(conn, known, clock)
        if activity_id not in known:
            return BatchCounters(failed=1)
        return _run(conn, [activity_id], clock)
