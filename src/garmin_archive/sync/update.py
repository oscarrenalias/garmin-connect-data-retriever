"""Update orchestration: discovery, then fetch of everything pending (SPEC §6)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from garmin_archive.archive import ArchiveReader, ArchiveWriter
from garmin_archive.models import (
    Clock,
    GarminArchiveError,
)
from garmin_archive.processing.batch import process_pending
from garmin_archive.sync.discovery import (
    DiscoveryOutcome,
    PageRecorder,
    SearchClient,
    discover_activities,
)
from garmin_archive.sync.fetch import FetchClient, fetch_pending
from garmin_archive.sync.runs import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    SyncCounters,
    derive_status,
    finish_run,
    start_run,
)

INVENTORY_COMPLETE = "complete"
INVENTORY_INCOMPLETE = "incomplete"

AGAIN_HINT = "Run `garmin-archive update` again to continue."
AGAIN_HINT_FULL = "Run `garmin-archive update --full` again to continue the full refresh."


class UpdateApi(SearchClient, FetchClient, Protocol):
    """The Garmin API surface an update needs (search plus per-resource fetches)."""


@dataclass
class UpdateResult:
    """Outcome of one ``update`` run."""

    run_id: int
    status: str
    counters: SyncCounters
    coverage: dict[str, dict[str, int]]
    inventory: str
    hint: str | None = None
    incomplete_reason: str | None = None
    pending: int = 0
    processed: int = 0
    processing_failed: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "counters": self.counters.to_dict(),
            "coverage": self.coverage,
            "inventory": self.inventory,
            "incomplete_reason": self.incomplete_reason,
            "pending": self.pending,
            "processed": self.processed,
            "processing_failed": self.processing_failed,
            "hint": self.hint,
        }


def _hint(status: str, full: bool) -> str | None:
    if status == STATUS_COMPLETE:
        return None
    return AGAIN_HINT_FULL if full and status == STATUS_INTERRUPTED else AGAIN_HINT


def _archived_anything(counters: SyncCounters) -> bool:
    return counters.discovered > 0 or counters.downloaded > 0 or counters.unchanged > 0


def run_update(
    api: UpdateApi,
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    full: bool = False,
    stop_flag: Callable[[], bool] = lambda: False,
) -> UpdateResult:
    """Run discovery, then fetch everything pending, even if discovery is incomplete.

    The caller owns the connection and the writer lock. The ``sync_runs`` row is
    finalized on every exit path; exceptions are re-raised after it is recorded. A run
    that ends ``failed`` because of an API error (nothing archived) re-raises that error.
    """
    writer = ArchiveWriter(conn, clock)
    reader = ArchiveReader(conn)
    counters = SyncCounters()
    run_id = start_run(conn, clock, full=full, parameters={"full": full})
    inventory = INVENTORY_INCOMPLETE
    try:
        recorder = PageRecorder(
            conn=conn,
            writer=writer,
            reader=reader,
            clock=clock,
            counters=counters,
            sync_run_id=run_id,
            full=full,
        )
        discovery: DiscoveryOutcome = discover_activities(
            api, conn, recorder, should_stop=stop_flag
        )
        if discovery.reached_end:
            inventory = INVENTORY_COMPLETE
        fetch = fetch_pending(
            api, conn, writer, reader, clock, run_id, counters, should_stop=stop_flag
        )
    except BaseException as exc:
        _finalize_on_error(conn, clock, reader, run_id, counters, inventory, exc)
        raise

    interrupted = fetch.interrupted or discovery.incomplete_reason == "interrupted"
    error: GarminArchiveError | None = discovery.last_error or fetch.rate_limited
    pending = len(reader.pending_resources())
    status = derive_status(
        reached_end=discovery.reached_end,
        counters=counters,
        pending=pending,
        stop_requested=interrupted,
        error=error,
        archived_anything=_archived_anything(counters),
    )
    coverage = reader.coverage()
    finish_run(
        conn,
        clock,
        run_id,
        status=status,
        counters=counters,
        coverage={"kinds": coverage, "inventory": inventory},
    )
    if status == STATUS_FAILED and error is not None:
        raise error
    # Processing runs after the sync row is finalized: it can never change sync status.
    try:
        batch = process_pending(conn, clock)
        processed, processing_failed = batch.processed + batch.partial, batch.failed
    except (GarminArchiveError, sqlite3.Error):
        processed, processing_failed = 0, 1
    return UpdateResult(
        run_id=run_id,
        status=status,
        counters=counters,
        coverage=coverage,
        inventory=inventory,
        hint=_hint(status, full),
        incomplete_reason=discovery.incomplete_reason,
        pending=pending,
        processed=processed,
        processing_failed=processing_failed,
    )


def _finalize_on_error(
    conn: sqlite3.Connection,
    clock: Clock,
    reader: ArchiveReader,
    run_id: int,
    counters: SyncCounters,
    inventory: str,
    exc: BaseException,
) -> None:
    """Best-effort ``finish_run`` for an exception; never masks the original error."""
    status = STATUS_INTERRUPTED if isinstance(exc, KeyboardInterrupt) else STATUS_FAILED
    try:
        coverage = reader.coverage()
    except Exception:
        coverage = {}
    with suppress(Exception):  # the original exception is what the caller must see
        finish_run(
            conn,
            clock,
            run_id,
            status=status,
            counters=counters,
            coverage={"kinds": coverage, "inventory": inventory},
        )
