"""Acceptance criteria 3-8 of spec-939594b5: per-activity processing against a real DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from garmin_archive.processing import inputs as inputs_module
from garmin_archive.processing.batch import process_pending
from garmin_archive.processing.processor import process_activity
from helpers.archive_seed import (
    FixedClock,
    raw_payload_digest,
    record_file,
    record_summary,
    search_item_copy,
    seed_activity,
    summary_copy,
)
from helpers.fitbuilder import FIT_START, build_activity_fit, cardio_fit, running_fit
from helpers.zipbuilder import build_zip

AID = "9000000001"
DERIVED = (
    "activities",
    "activity_sessions",
    "activity_laps",
    "activity_samples",
    "activity_sets",
)


def _zip(fit: bytes) -> bytes:
    return build_zip({f"{AID}_ACTIVITY.fit": fit})


def _count(conn: sqlite3.Connection, table: str, aid: str = AID) -> int:
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE activity_id = ?", (aid,)).fetchone()[0]
    )


def _counts(conn: sqlite3.Connection, aid: str = AID) -> dict[str, int]:
    return {t: _count(conn, t, aid) for t in DERIVED}


def _activity(conn: sqlite3.Connection, aid: str = AID) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM activities WHERE activity_id = ?", (aid,)).fetchone()
    finally:
        conn.row_factory = None
    return dict(row)


def _run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    try:
        return dict(
            conn.execute("SELECT * FROM processing_runs WHERE id = ?", (run_id,)).fetchone()
        )
    finally:
        conn.row_factory = None


def _fk_ok(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---- criterion 3 ----------------------------------------------------------------------


def test_running_activity_end_to_end(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("running-1", AID),
        original_file=_zip(running_fit()),
        search_item=search_item_copy("running-1", AID),
    )
    result = process_activity(conn, AID, clock)
    assert result.status == "processed"
    assert result.warnings == ()
    assert _counts(conn) == {
        "activities": 1,
        "activity_sessions": 1,
        "activity_laps": 3,
        "activity_samples": 100,
        "activity_sets": 0,
    }
    row = _activity(conn)
    assert row["type_key"] == "running"
    assert row["sport_family"] == "running"
    assert row["start_at_utc"] == "2026-06-25T06:42:28Z"
    assert row["start_local"] == "2026-06-25T09:42:28"
    assert row["local_date"] == "2026-06-25"
    assert row["timezone"] == "Europe/Athens"
    assert row["distance_m"] == 5573.0
    assert row["avg_hr_bpm"] == 134.0
    assert row["processing_status"] == "processed"
    assert row["processed_at_utc"] == "2026-07-01T12:00:00.000000Z"

    lat = conn.execute(
        "SELECT sample_index, latitude_deg, longitude_deg FROM activity_samples"
        " WHERE activity_id = ? ORDER BY sample_index",
        (AID,),
    ).fetchall()
    for index, lat_deg, lon_deg in lat:
        assert lat_deg == pytest.approx((400_000_000 + index * 1000) * 180 / 2**31, abs=1e-6)
        assert lon_deg == pytest.approx((-300_000_000 + index * 500) * 180 / 2**31, abs=1e-6)
    nulls = conn.execute(
        "SELECT COUNT(*) FROM activity_samples WHERE activity_id = ? AND (power_w IS NOT NULL"
        " OR cadence IS NOT NULL OR temperature_c IS NOT NULL)",
        (AID,),
    ).fetchone()[0]
    assert nulls == 0
    assert _fk_ok(conn)


def test_run_row_records_clock_version_and_provenance(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    clock.current = FIT_START  # a different instant from seeding
    result = process_activity(conn, AID, clock)
    run = _run(conn, result.run_id)
    assert run["status"] == "processed"
    assert run["started_at_utc"] == "2026-06-25T06:42:28.000000Z"
    assert run["finished_at_utc"] == "2026-06-25T06:42:28.000000Z"
    assert run["processor_version"].endswith("+sports2")
    assert run["error_code"] is None
    assert run["warnings_json"] is None
    assert {r[0] for r in conn.execute("SELECT processing_run_id FROM activity_samples")} == {
        result.run_id
    }
    file_id = conn.execute(
        "SELECT current_payload_id FROM activity_resources WHERE kind = 'original_file'"
    ).fetchone()[0]
    assert {r[0] for r in conn.execute("SELECT source_payload_id FROM activity_laps")} == {file_id}


# ---- criterion 4 ----------------------------------------------------------------------


def test_cardio_activity_sets_without_laps(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("cardio-1", AID), original_file=_zip(cardio_fit())
    )
    assert process_activity(conn, AID, clock).status == "processed"
    counts = _counts(conn)
    assert counts["activity_sets"] == 10
    assert counts["activity_laps"] == 0
    assert counts["activity_sessions"] == 1
    assert _activity(conn)["sport_family"] == "cardio"
    assert _fk_ok(conn)


# ---- criterion 5 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "file_body", "warning"),
    [
        ("missing", None, "original_file_missing"),
        ("corrupt_zip", b"this is not a zip", "fit_unavailable"),
        ("undecodable_fit", build_zip({"a.fit": b"not fit data at all"}), "fit_unavailable"),
        ("no_fit_member", build_zip({"a.txt": b"x"}), "fit_unavailable"),
    ],
)
def test_unusable_file_gives_partial_with_json_row(
    conn: sqlite3.Connection,
    clock: FixedClock,
    label: str,
    file_body: bytes | None,
    warning: str,
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=file_body)
    digest = raw_payload_digest(conn)
    result = process_activity(conn, AID, clock)
    assert result.status == "partial", label
    row = _activity(conn)
    assert row["processing_status"] == "partial"
    assert row["distance_m"] == 5573.0
    assert row["start_at_utc"] == "2026-06-25T06:42:28Z"
    counts = _counts(conn)
    assert counts["activities"] == 1
    assert counts["activity_sessions"] == counts["activity_laps"] == counts["activity_samples"] == 0
    warnings = json.loads(_run(conn, result.run_id)["warnings_json"])
    assert any(w.startswith(warning) for w in warnings)
    assert raw_payload_digest(conn) == digest


def test_summary_less_activity_with_search_item_only_is_partial(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn,
        clock,
        AID,
        search_item=search_item_copy("running-1", AID),
        original_file=_zip(running_fit()),
    )
    result = process_activity(conn, AID, clock)
    assert result.status == "partial"
    assert "summary_missing" in result.warnings
    row = _activity(conn)
    assert row["activity_id"] == AID
    assert row["type_key"] == "running"
    assert row["start_at_utc"] == "2026-06-25T06:42:28Z"
    assert row["distance_m"] == 5573.0
    assert _counts(conn)["activity_samples"] == 100


def test_summary_not_json_object_is_partial(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID, summary=b"[1, 2]")
    result = process_activity(conn, AID, clock)
    assert result.status == "partial"
    assert "summary_not_object" in result.warnings
    assert _activity(conn)["activity_id"] == AID


def test_laps_without_session_is_partial_and_stores_no_laps(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    fit = build_activity_fit(
        laps=[{"start_time": FIT_START, "timestamp": FIT_START, "total_elapsed_time": 5.0}],
        records=[{"timestamp": FIT_START, "heart_rate": 90}],
    )
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(fit))
    result = process_activity(conn, AID, clock)
    assert result.status == "partial"
    assert "laps_without_session" in result.warnings
    counts = _counts(conn)
    assert counts["activity_laps"] == 0
    assert counts["activity_samples"] == 1


# ---- criterion 6 ----------------------------------------------------------------------


def test_unchanged_inputs_are_not_reprocessed(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    first = process_pending(conn, clock)
    assert (first.processed, first.partial, first.failed) == (1, 0, 0)
    before = _counts(conn)
    second = process_pending(conn, clock)
    assert (second.processed, second.partial, second.failed) == (0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0] == 1
    assert _counts(conn) == before


def test_new_payload_version_replaces_derived_rows(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    process_pending(conn, clock)
    old_run = conn.execute("SELECT MAX(id) FROM processing_runs").fetchone()[0]
    clock.current += timedelta(minutes=5)
    record_summary(conn, clock, AID, summary_copy("running-1", AID, activityName="renamed"))
    record_file(conn, clock, AID, _zip(running_fit(lap_count=2, record_count=40)))
    assert process_pending(conn, clock).processed == 1
    assert _counts(conn) == {
        "activities": 1,
        "activity_sessions": 1,
        "activity_laps": 2,
        "activity_samples": 40,
        "activity_sets": 0,
    }
    assert _activity(conn)["name"] == "renamed"
    new_run = conn.execute("SELECT MAX(id) FROM processing_runs").fetchone()[0]
    assert new_run != old_run
    for table in DERIVED[:-1]:  # activity_sets is empty for a running file
        assert {r[0] for r in conn.execute(f"SELECT processing_run_id FROM {table}")} == {new_run}
    assert _fk_ok(conn)


def test_bumped_processor_version_reprocesses(
    conn: sqlite3.Connection, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    process_pending(conn, clock)
    assert process_pending(conn, clock).processed == 0
    monkeypatch.setattr(inputs_module, "SPORTS_VERSION", "3")
    assert process_pending(conn, clock).processed == 1
    versions = [
        r[0] for r in conn.execute("SELECT processor_version FROM processing_runs ORDER BY id")
    ]
    assert versions[0].endswith("+sports2")
    assert versions[1].endswith("+sports3")
    assert _counts(conn)["activity_samples"] == 100  # no duplicates


def test_file_replaced_by_unusable_one_leaves_no_stale_rows(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    process_pending(conn, clock)
    clock.current += timedelta(minutes=1)
    record_file(conn, clock, AID, b"now broken")
    assert process_pending(conn, clock).partial == 1
    counts = _counts(conn)
    assert counts["activities"] == 1
    assert counts["activity_sessions"] == counts["activity_laps"] == counts["activity_samples"] == 0
    assert _activity(conn)["processing_status"] == "partial"


# ---- criterion 7 ----------------------------------------------------------------------


def test_injected_failure_mid_replace_keeps_previous_rows(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    ok = process_activity(conn, AID, clock)
    assert ok.status == "processed"
    before = _counts(conn)
    before_laps = conn.execute("SELECT * FROM activity_laps ORDER BY lap_index").fetchall()
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON activity_laps"
        " BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    conn.commit()
    clock.current += timedelta(minutes=1)
    record_summary(conn, clock, AID, summary_copy("running-1", AID, activityName="new name"))
    failed = process_activity(conn, AID, clock)
    assert failed.status == "failed"
    assert failed.error_code == "STORAGE_ERROR"
    assert _counts(conn) == before
    assert conn.execute("SELECT * FROM activity_laps ORDER BY lap_index").fetchall() == before_laps
    row = _activity(conn)
    assert row["processing_run_id"] == ok.run_id
    assert row["name"] != "new name"
    run = _run(conn, failed.run_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "STORAGE_ERROR"
    assert run["finished_at_utc"] is not None
    assert "injected" not in json.dumps(run)
    assert not conn.in_transaction


def test_failed_run_does_not_make_activity_not_pending(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON activity_samples BEGIN SELECT RAISE(ABORT, 'x'); END"
    )
    conn.commit()
    assert process_pending(conn, clock).failed == 1
    assert process_pending(conn, clock).failed == 1  # retried every call
    assert _count(conn, "activities") == 0


# ---- criterion 8 ----------------------------------------------------------------------


def test_multisport_parent_sessions_only_on_parent(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    parent, child_a, child_b = "9000000010", "9000000011", "9000000012"
    sessions = [
        {
            "start_time": FIT_START + timedelta(seconds=100 * i),
            "timestamp": FIT_START + timedelta(seconds=100 * (i + 1)),
            "sport": sport,
            "total_elapsed_time": 100.0,
        }
        for i, sport in enumerate(["swimming", "cycling"])
    ]
    records = [{"timestamp": FIT_START + timedelta(seconds=i)} for i in range(5)]
    seed_activity(
        conn,
        clock,
        parent,
        summary=summary_copy("running-1", parent, isMultiSportParent=True),
        original_file=build_zip({"p.fit": build_activity_fit(sessions=sessions, records=records)}),
    )
    for child in (child_a, child_b):
        seed_activity(
            conn,
            clock,
            child,
            summary=summary_copy("running-1", child, parentId=int(parent)),
            parent_activity_id=parent,
        )
    batch = process_pending(conn, clock)
    assert batch.failed == 0
    assert _count(conn, "activity_sessions", parent) == 2
    assert _count(conn, "activity_samples", parent) == 5
    for child in (child_a, child_b):
        assert _count(conn, "activity_sessions", child) == 0
        assert _count(conn, "activity_laps", child) == 0
        assert _count(conn, "activity_samples", child) == 0
        assert _count(conn, "activities", child) == 1
    assert _activity(conn, parent)["is_multisport_parent"] == 1
    assert _activity(conn, parent)["parent_activity_id"] is None
    assert _activity(conn, child_a)["parent_activity_id"] == parent
    assert _fk_ok(conn)
