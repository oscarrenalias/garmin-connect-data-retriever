"""Acceptance criteria 2-7 and 9 of spec-2699f0ff: sport metrics through the processor."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import pytest

from garmin_archive.processing.batch import process_pending
from garmin_archive.processing.fit_rows import sample_rows
from garmin_archive.processing.processor import process_activity
from helpers.archive_seed import (
    FixedClock,
    record_file,
    seed_activity,
    summary_copy,
)
from helpers.fitbuilder import FIT_START, build_activity_fit, cardio_fit, running_fit
from helpers.zipbuilder import build_zip

AID = "9100000001"
METRIC_TABLES = ("running_metrics", "swimming_metrics", "cycling_metrics", "cardio_metrics")


def _zip(fit: bytes, aid: str = AID) -> bytes:
    return build_zip({f"{aid}_ACTIVITY.fit": fit})


def _row(conn: sqlite3.Connection, table: str, aid: str = AID) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(f"SELECT * FROM {table} WHERE activity_id = ?", (aid,)).fetchone()
    finally:
        conn.row_factory = None
    assert found is not None, table
    return dict(found)


def _count(conn: sqlite3.Connection, table: str, aid: str = AID) -> int:
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE activity_id = ?", (aid,)).fetchone()[0]
    )


def _length(
    index: int,
    kind: str,
    stroke: str,
    timer: float,
    strokes: int,
    *,
    start: datetime = FIT_START,
) -> dict[str, Any]:
    begin = start + timedelta(seconds=index * 40)
    return {
        "timestamp": begin + timedelta(seconds=timer),
        "start_time": begin,
        "total_elapsed_time": timer + 1.0,
        "total_timer_time": timer,
        "length_type": kind,
        "swim_stroke": stroke,
        "total_strokes": strokes,
        "avg_swimming_cadence": 20,
    }


# 4 active (2 freestyle, 1 breaststroke, 1 drill without strokes) and 2 idle lengths.
POOL_LENGTHS = [
    _length(0, "active", "freestyle", 20.5, 18),
    _length(1, "active", "freestyle", 21.0, 19),
    _length(2, "idle", "freestyle", 15.0, 0),
    _length(3, "active", "breaststroke", 30.0, 14),
    _length(4, "active", "drill", 28.0, 0),
    _length(5, "idle", "freestyle", 10.0, 0),
]


def _swim_fit(lengths: list[dict[str, Any]] = POOL_LENGTHS) -> bytes:
    end = FIT_START + timedelta(seconds=len(lengths) * 40)
    lap = {
        "timestamp": end,
        "start_time": FIT_START,
        "total_elapsed_time": len(lengths) * 40.0,
        "total_timer_time": len(lengths) * 40.0,
    }
    session = {
        "timestamp": end,
        "start_time": FIT_START,
        "sport": "swimming",
        "sub_sport": "lap_swimming",
        "total_elapsed_time": len(lengths) * 40.0,
        "total_timer_time": len(lengths) * 40.0,
    }
    return build_activity_fit(sessions=[session], laps=[lap], lengths=lengths)


def _seed_swim(
    conn: sqlite3.Connection, clock: FixedClock, *, fixture: str = "pool_swim-1", **overrides: Any
) -> None:
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy(fixture, AID, **overrides),
        original_file=_zip(_swim_fit()),
    )


# ---- criterion 2: running ---------------------------------------------------------------


def test_running_metrics_from_summary(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("running-1", AID), original_file=_zip(running_fit())
    )
    assert process_activity(conn, AID, clock).status == "processed"
    row = _row(conn, "running_metrics")
    assert row["avg_cadence_spm"] == pytest.approx(148.25)  # not doubled
    assert row["avg_stride_length_m"] == pytest.approx(0.8859, abs=1e-4)
    assert row["avg_ground_contact_time_ms"] == pytest.approx(306.9, abs=0.01)
    assert row["avg_vertical_ratio_pct"] == pytest.approx(8.99, abs=0.01)
    assert row["processing_run_id"] > 0
    assert row["source_payload_id"] is not None


def test_running_sample_cadence_is_spm_with_fractional() -> None:
    records = [{"timestamp": FIT_START, "cadence": 74, "fractional_cadence": 0.125}]
    row = sample_rows(records, running=True)[0]
    assert row["cadence"] == pytest.approx(148.25)
    assert row["cadence_unit"] == "spm"


def test_processor_stores_running_sample_cadence_as_spm(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    records = [
        {"timestamp": FIT_START + timedelta(seconds=i), "cadence": 74, "fractional_cadence": 0.125}
        for i in range(3)
    ]
    session = {"timestamp": FIT_START, "start_time": FIT_START, "sport": "running"}
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("running-1", AID),
        original_file=_zip(build_activity_fit(sessions=[session], records=records)),
    )
    process_activity(conn, AID, clock)
    rows = conn.execute(
        "SELECT cadence, cadence_unit FROM activity_samples WHERE activity_id = ?", (AID,)
    ).fetchall()
    assert [r[1] for r in rows] == ["spm"] * 3
    assert [r[0] for r in rows] == [pytest.approx(148.25)] * 3


def test_non_running_sample_cadence_stays_fit_rpm(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    records = [{"timestamp": FIT_START, "cadence": 80}]
    session = {"timestamp": FIT_START, "start_time": FIT_START, "sport": "cycling"}
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("running-1", AID, activityTypeDTO={"typeKey": "cycling"}),
        original_file=_zip(build_activity_fit(sessions=[session], records=records)),
    )
    process_activity(conn, AID, clock)
    assert conn.execute(
        "SELECT cadence, cadence_unit FROM activity_samples WHERE activity_id = ?", (AID,)
    ).fetchall() == [(80.0, "fit_rpm")]


# ---- criterion 3: pool swim -------------------------------------------------------------


def test_pool_swim_lengths_and_metrics(conn: sqlite3.Connection, clock: FixedClock) -> None:
    _seed_swim(conn, clock)
    result = process_activity(conn, AID, clock)
    assert result.status == "processed", result.warnings
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM swim_lengths WHERE activity_id = ? ORDER BY length_index", (AID,)
        )
    ]
    conn.row_factory = None
    assert [r["length_index"] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert [r["length_type"] for r in rows] == [
        "active",
        "active",
        "idle",
        "active",
        "active",
        "idle",
    ]
    assert [r["stroke_type"] for r in rows][:2] == ["freestyle", "freestyle"]
    assert rows[3]["stroke_type"] == "breaststroke"
    assert [r["distance_m"] for r in rows] == [25.0, 25.0, None, 25.0, 25.0, None]
    assert [r["lap_index"] for r in rows] == [0] * 6
    assert rows[0]["swolf"] == pytest.approx(20.5 + 18)
    assert rows[3]["swolf"] == pytest.approx(44.0)
    assert rows[4]["swolf"] is None  # drill, no strokes
    assert rows[2]["swolf"] is None  # idle
    assert rows[0]["start_at_utc"] == "2026-06-25T06:42:28Z"

    metrics = _row(conn, "swimming_metrics")
    assert metrics["swim_environment"] == "pool"
    assert metrics["pool_length_m"] == pytest.approx(25.0)
    assert metrics["active_lengths"] == 55  # copied from the summary, not recomputed
    assert metrics["total_strokes"] == 510
    assert metrics["avg_swolf"] == 39.0
    assert metrics["active_swim_duration_s"] == pytest.approx(20.5 + 21.0 + 30.0 + 28.0)
    assert metrics["recorded_rest_duration_s"] == pytest.approx(25.0)
    assert metrics["length_coverage_status"] == "complete"


def test_pool_swim_active_lengths_counted_from_rows(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    _seed_swim(conn, clock)
    process_activity(conn, AID, clock)
    count = conn.execute(
        "SELECT COUNT(*) FROM swim_lengths WHERE activity_id = ? AND length_type = 'active'",
        (AID,),
    ).fetchone()[0]
    assert count == 4


def test_25_yard_pool_is_converted_to_metres(conn: sqlite3.Connection, clock: FixedClock) -> None:
    summary = json.loads(summary_copy("pool_swim-1", AID))
    summary["summaryDTO"]["unitOfPoolLength"] = {"unitId": 3, "unitKey": "yard", "factor": 9144.0}
    seed_activity(
        conn,
        clock,
        AID,
        summary=json.dumps(summary).encode(),
        original_file=_zip(_swim_fit()),
    )
    process_activity(conn, AID, clock)
    assert _row(conn, "swimming_metrics")["pool_length_m"] == pytest.approx(22.86)
    distances = [
        r[0]
        for r in conn.execute(
            "SELECT distance_m FROM swim_lengths WHERE activity_id = ? AND length_type = 'active'",
            (AID,),
        )
    ]
    assert distances == [pytest.approx(22.86)] * 4


def test_unknown_pool_unit_gives_partial_and_null_pool_length(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    summary = json.loads(summary_copy("pool_swim-1", AID))
    summary["summaryDTO"]["unitOfPoolLength"] = {"unitKey": "furlong"}
    seed_activity(
        conn,
        clock,
        AID,
        summary=json.dumps(summary).encode(),
        original_file=_zip(_swim_fit()),
    )
    result = process_activity(conn, AID, clock)
    assert result.status == "partial"
    assert [w for w in result.warnings if w.startswith("unknown_pool_length_unit")] == [
        "unknown_pool_length_unit: 'furlong'"
    ]
    assert _row(conn, "swimming_metrics")["pool_length_m"] is None


def test_pool_swim_without_fit_has_coverage_none(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("pool_swim-1", AID))
    process_activity(conn, AID, clock)
    metrics = _row(conn, "swimming_metrics")
    assert metrics["length_coverage_status"] == "none"
    assert metrics["active_swim_duration_s"] is None
    assert metrics["recorded_rest_duration_s"] is None
    assert _count(conn, "swim_lengths") == 0


# ---- criterion 4: open water ------------------------------------------------------------


def test_open_water_has_no_pool_length_or_lengths(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("open_water_swim-1", AID),
        original_file=_zip(_swim_fit()),  # even if lengths were present they are not stored
    )
    process_activity(conn, AID, clock)
    metrics = _row(conn, "swimming_metrics")
    assert metrics["swim_environment"] == "open_water"
    assert metrics["pool_length_m"] is None
    assert _count(conn, "swim_lengths") == 0
    assert metrics["total_strokes"] == 300


# ---- criterion 5: cycling ---------------------------------------------------------------


def test_cycling_without_power_or_cadence_is_null(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    summary = json.loads(summary_copy("running-1", AID))
    summary["activityTypeDTO"] = {"typeKey": "road_biking"}
    for key in (
        "averageRunCadence",
        "maxRunCadence",
        "averageBikeCadence",
        "maxBikeCadence",
        "averagePower",
        "maxPower",
        "normalizedPower",
        "trainingStressScore",
        "intensityFactor",
    ):
        summary["summaryDTO"].pop(key, None)
    seed_activity(conn, clock, AID, summary=json.dumps(summary).encode())
    process_activity(conn, AID, clock)
    row = _row(conn, "cycling_metrics")
    for column in (
        "avg_cadence_rpm",
        "max_cadence_rpm",
        "avg_power_w",
        "max_power_w",
        "normalized_power_w",
        "training_stress_score",
        "intensity_factor",
    ):
        assert row[column] is None, column
    assert _count(conn, "running_metrics") == 0


# ---- criterion 6: cardio ----------------------------------------------------------------


def test_cardio_metrics_from_summary_and_sets(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(
        conn, clock, AID, summary=summary_copy("cardio-1", AID), original_file=_zip(cardio_fit())
    )
    assert process_activity(conn, AID, clock).status == "processed"
    row = _row(conn, "cardio_metrics")
    assert row["steps"] == 1184
    assert row["total_sets"] == 34
    assert row["active_sets"] == 17
    # cardio_fit: 10 sets, even index active (reps 10, 12, 14, 16, 18), odd rest, 30 s each
    assert row["total_reps"] == 70
    assert row["active_set_duration_s"] == pytest.approx(150.0)
    assert row["rest_set_duration_s"] == pytest.approx(150.0)


def test_cardio_without_fit_has_null_set_sums(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("cardio-1", AID))
    process_activity(conn, AID, clock)
    row = _row(conn, "cardio_metrics")
    assert row["steps"] == 1184
    assert row["total_reps"] is None
    assert row["active_set_duration_s"] is None
    assert row["rest_set_duration_s"] is None


# ---- criterion 7: families without metrics ----------------------------------------------


@pytest.mark.parametrize(
    ("type_key", "family"), [("hiit", "hiit"), ("walking", "walking"), ("mobility", "other")]
)
def test_other_families_get_no_metrics_but_are_in_summary_view(
    conn: sqlite3.Connection, clock: FixedClock, type_key: str, family: str
) -> None:
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("other-1", AID, activityTypeDTO={"typeKey": type_key}),
    )
    process_activity(conn, AID, clock)
    for table in (*METRIC_TABLES, "swim_lengths"):
        assert _count(conn, table) == 0, table
    row = conn.execute(
        "SELECT sport_family, include_in_volume_totals FROM v_activity_summary"
        " WHERE activity_id = ?",
        (AID,),
    ).fetchone()
    assert row == (family, 1)


# ---- criterion 9: atomic replace --------------------------------------------------------


def test_reprocessing_leaves_no_duplicate_metrics_or_lengths(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    _seed_swim(conn, clock)
    first = process_activity(conn, AID, clock)
    second = process_activity(conn, AID, clock)
    assert first.run_id != second.run_id
    assert _count(conn, "swimming_metrics") == 1
    assert _count(conn, "swim_lengths") == 6
    run_ids = {r[0] for r in conn.execute("SELECT processing_run_id FROM swim_lengths")}
    assert run_ids == {second.run_id}
    assert _row(conn, "swimming_metrics")["processing_run_id"] == second.run_id


@pytest.mark.parametrize(
    ("fixture", "table"),
    [
        ("running-1", "running_metrics"),
        ("cardio-1", "cardio_metrics"),
        ("pool_swim-1", "swimming_metrics"),
    ],
)
def test_reprocessing_metrics_tables_have_one_row(
    conn: sqlite3.Connection, clock: FixedClock, fixture: str, table: str
) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy(fixture, AID))
    process_activity(conn, AID, clock)
    process_activity(conn, AID, clock)
    assert _count(conn, table) == 1


def test_sport_change_removes_old_metrics_row(conn: sqlite3.Connection, clock: FixedClock) -> None:
    from helpers.archive_seed import record_summary

    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID))
    process_activity(conn, AID, clock)
    assert _count(conn, "running_metrics") == 1
    record_summary(
        conn, clock, AID, summary_copy("running-1", AID, activityTypeDTO={"typeKey": "cycling"})
    )
    process_activity(conn, AID, clock)
    assert _count(conn, "running_metrics") == 0
    assert _count(conn, "cycling_metrics") == 1


def test_replaced_file_without_lengths_drops_old_length_rows(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    _seed_swim(conn, clock)
    process_activity(conn, AID, clock)
    record_file(conn, clock, AID, _zip(_swim_fit(POOL_LENGTHS[:2])))
    assert process_pending(conn, clock).processed == 1
    assert _count(conn, "swim_lengths") == 2
    assert _row(conn, "swimming_metrics")["recorded_rest_duration_s"] == pytest.approx(0.0)


def test_processor_version_reflects_sports2(conn: sqlite3.Connection, clock: FixedClock) -> None:
    seed_activity(conn, clock, AID, summary=summary_copy("running-1", AID))
    result = process_activity(conn, AID, clock)
    version = conn.execute(
        "SELECT processor_version FROM processing_runs WHERE id = ?", (result.run_id,)
    ).fetchone()[0]
    assert version.endswith("+sports2")
