from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from garmin_archive.models import ArchiveLockedError, StorageError
from garmin_archive.processing import batch as batch_module
from garmin_archive.processing.batch import process_pending, reprocess
from garmin_archive.processing.processor import ProcessingResult
from garmin_archive.storage.lock import writer_lock
from helpers.archive_seed import FixedClock, record_summary, seed_activity, summary_copy
from helpers.fitbuilder import running_fit
from helpers.zipbuilder import build_zip

IDS = ("9000000001", "9000000002", "9000000003")


@pytest.fixture
def seeded(conn: sqlite3.Connection, clock: FixedClock) -> sqlite3.Connection:
    for aid in IDS:
        seed_activity(
            conn,
            clock,
            aid,
            summary=summary_copy("running-1", aid),
            original_file=build_zip({"a.fit": running_fit()}),
        )
    return conn


def _runs(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0])


def test_process_pending_after_full_run_does_nothing(
    seeded: sqlite3.Connection, clock: FixedClock
) -> None:
    first = process_pending(seeded, clock)
    assert (first.processed, first.partial, first.failed) == (3, 0, 0)
    second = process_pending(seeded, clock)
    assert (second.processed, second.partial, second.failed) == (0, 0, 0)
    assert _runs(seeded) == 3


def test_process_pending_reprocesses_changed_activity_only(
    seeded: sqlite3.Connection, clock: FixedClock
) -> None:
    process_pending(seeded, clock)
    record_summary(seeded, clock, IDS[1], summary_copy("running-1", IDS[1], activityName="x"))
    counters = process_pending(seeded, clock)
    assert counters.processed == 1
    assert _runs(seeded) == 4
    assert seeded.execute(
        "SELECT name FROM activities WHERE activity_id = ?", (IDS[1],)
    ).fetchone() == ("x",)


def test_reprocess_all_ignores_fingerprints(seeded: sqlite3.Connection, clock: FixedClock) -> None:
    process_pending(seeded, clock)
    counters = reprocess(seeded, clock)
    assert (counters.processed, counters.partial, counters.failed) == (3, 0, 0)
    assert _runs(seeded) == 6
    assert seeded.execute("SELECT COUNT(*) FROM activity_samples").fetchone()[0] == 300


def test_reprocess_single_activity(seeded: sqlite3.Connection, clock: FixedClock) -> None:
    process_pending(seeded, clock)
    counters = reprocess(seeded, clock, IDS[2])
    assert (counters.processed, counters.partial, counters.failed) == (1, 0, 0)
    assert _runs(seeded) == 4
    assert seeded.execute(
        "SELECT COUNT(*) FROM processing_runs WHERE activity_id = ?", (IDS[2],)
    ).fetchone() == (2,)


def test_reprocess_unknown_id_counts_one_failure_and_no_run(
    seeded: sqlite3.Connection, clock: FixedClock
) -> None:
    counters = reprocess(seeded, clock, "123")
    assert (counters.processed, counters.partial, counters.failed) == (0, 0, 1)
    assert _runs(seeded) == 0


def test_partial_activities_counted_as_partial(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, IDS[0], summary=summary_copy("running-1", IDS[0]))
    counters = process_pending(conn, clock)
    assert (counters.processed, counters.partial, counters.failed) == (0, 1, 0)


def test_failing_activity_does_not_stop_the_batch(
    seeded: sqlite3.Connection, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = batch_module.process_activity

    def flaky(conn: sqlite3.Connection, activity_id: str, clock_: FixedClock) -> ProcessingResult:
        if activity_id == IDS[1]:
            raise StorageError("boom")
        return real(conn, activity_id, clock_)

    monkeypatch.setattr(batch_module, "process_activity", flaky)
    counters = process_pending(seeded, clock)
    assert (counters.processed, counters.partial, counters.failed) == (2, 0, 1)
    done = {r[0] for r in seeded.execute("SELECT activity_id FROM activities")}
    assert done == {IDS[0], IDS[2]}


def test_failed_activity_via_trigger_is_counted_failed(
    seeded: sqlite3.Connection, clock: FixedClock
) -> None:
    seeded.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON activity_laps WHEN NEW.activity_id = '9000000002'"
        " BEGIN SELECT RAISE(ABORT, 'x'); END"
    )
    seeded.commit()
    counters = reprocess(seeded, clock)
    assert (counters.processed, counters.partial, counters.failed) == (2, 0, 1)


def test_reprocess_raises_when_another_holder_has_the_lock(
    conn: sqlite3.Connection, clock: FixedClock, db_path: Path
) -> None:
    seed_activity(conn, clock, IDS[0], summary=summary_copy("running-1", IDS[0]))
    with writer_lock(db_path.resolve()), pytest.raises(ArchiveLockedError):
        reprocess(conn, clock)
    assert _runs(conn) == 0
    assert reprocess(conn, clock).partial == 1  # lock released afterwards
