from __future__ import annotations

import sqlite3
from datetime import timedelta

from garmin_archive.processing.batch import process_pending
from garmin_archive.processing.inputs import (
    input_fingerprint,
    load_inputs,
    pending_activity_ids,
    processor_version,
)
from garmin_archive.processing.sports import SPORTS_VERSION
from helpers.archive_seed import (
    FixedClock,
    record_summary,
    search_item_copy,
    seed_activity,
    summary_copy,
)
from helpers.fitbuilder import running_fit
from helpers.zipbuilder import build_zip

AID = "9000000001"


def _zip() -> bytes:
    return build_zip({"a.fit": running_fit()})


def test_fingerprint_depends_on_each_input() -> None:
    base = input_fingerprint(1, 2, "v")
    assert input_fingerprint(1, 2, "v") == base
    assert input_fingerprint(3, 2, "v") != base
    assert input_fingerprint(1, 3, "v") != base
    assert input_fingerprint(1, 2, "w") != base
    assert input_fingerprint(None, None, "v") != base


def test_processor_version_includes_sports_version() -> None:
    assert processor_version().endswith(f"+sports{SPORTS_VERSION}")


def test_load_inputs_with_everything(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("running-1", AID),
        original_file=_zip(),
        search_item=search_item_copy("running-1", AID),
    )
    inputs = load_inputs(conn, AID)
    assert inputs.summary is not None
    assert inputs.original_file is not None
    assert isinstance(inputs.search_item, dict)
    assert inputs.search_item_payload_id is not None
    assert inputs.fingerprint == input_fingerprint(
        inputs.summary.id, inputs.original_file.id, processor_version()
    )


def test_load_inputs_with_nothing(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID)
    inputs = load_inputs(conn, AID)
    assert inputs.summary is None
    assert inputs.original_file is None
    assert inputs.search_item is None
    assert inputs.search_item_payload_id is None
    assert inputs.summary_payload_id is None
    assert inputs.file_payload_id is None


def test_search_item_change_does_not_change_fingerprint(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip())
    before = load_inputs(conn, AID).fingerprint
    item = search_item_copy("running-1", AID)
    item["activityName"] = "changed"
    seed_activity(conn, clock, AID, search_item=item)
    after = load_inputs(conn, AID)
    assert after.search_item is not None
    assert after.fingerprint == before


def test_new_summary_version_changes_fingerprint(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID))
    before = load_inputs(conn, AID).fingerprint
    record_summary(conn, clock, AID, summary_copy("running-1", AID, activityName="v2"))
    assert load_inputs(conn, AID).fingerprint != before


def test_pending_lifecycle(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip())
    assert pending_activity_ids(conn) == [AID]  # no run yet
    process_pending(conn, clock)
    assert pending_activity_ids(conn) == []
    clock.current += timedelta(minutes=1)
    record_summary(conn, clock, AID, summary_copy("running-1", AID, activityName="v2"))
    assert pending_activity_ids(conn) == [AID]  # payload changed after run


def test_partial_with_unchanged_inputs_is_not_pending(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID))  # no file -> partial
    process_pending(conn, clock)
    status = conn.execute("SELECT status FROM processing_runs").fetchone()[0]
    assert status == "partial"
    assert pending_activity_ids(conn) == []


def test_processor_version_change_makes_pending(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip())
    process_pending(conn, clock)
    conn.execute(
        "UPDATE processing_runs SET input_fingerprint = ?", (input_fingerprint(1, 1, "old"),)
    )
    conn.commit()
    assert pending_activity_ids(conn) == [AID]


def test_failed_only_runs_are_pending(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip())
    process_pending(conn, clock)
    conn.execute("UPDATE processing_runs SET status = 'failed'")
    conn.commit()
    assert pending_activity_ids(conn) == [AID]


def test_latest_processed_run_decides(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip())
    process_pending(conn, clock)
    current = conn.execute("SELECT input_fingerprint FROM processing_runs").fetchone()[0]
    # An older run with a stale fingerprint must not matter; a newer failed run is ignored.
    conn.execute(
        "INSERT INTO processing_runs (activity_id, processor_version, input_fingerprint, status,"
        " started_at_utc) VALUES (?, 'x', 'stale', 'processed', '2000-01-01T00:00:00Z')",
        (AID,),
    )
    conn.execute(
        "INSERT INTO processing_runs (activity_id, processor_version, input_fingerprint, status,"
        " started_at_utc) VALUES (?, 'x', 'newer-failed', 'failed', '2099-01-01T00:00:00Z')",
        (AID,),
    )
    conn.commit()
    assert current != "stale"
    assert pending_activity_ids(conn) == []
