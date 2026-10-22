"""Heuristic ``likely_unrecorded_swim`` flag on idle pool lengths and its metrics/views."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from garmin_archive.processing.processor import process_activity
from garmin_archive.processing.swim import HR_RATIO, MIN_IDLE_S
from helpers.archive_seed import FixedClock, seed_activity, summary_copy
from helpers.fitbuilder import FIT_START, build_activity_fit
from helpers.zipbuilder import build_zip

AID = "9100000002"

# (start offset s, duration s, type, bpm or None for no samples)
SEGMENTS: list[tuple[int, int, str, int | None]] = [
    (0, 30, "active", 140),
    (30, 30, "active", 140),
    (60, 90, "idle", 135),  # flagged
    (150, 90, "idle", 100),  # recovery, not flagged
    (240, 30, "active", 140),
    (270, 20, "idle", 138),  # too short
    (290, 30, "active", 140),
    (320, 90, "idle", None),  # no HR samples
]


def _fit() -> bytes:
    lengths: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for start, duration, kind, bpm in SEGMENTS:
        begin = FIT_START + timedelta(seconds=start)
        lengths.append(
            {
                "timestamp": begin + timedelta(seconds=duration),
                "start_time": begin,
                "total_elapsed_time": float(duration),
                "total_timer_time": float(duration),
                "length_type": kind,
                "swim_stroke": "freestyle",
                "total_strokes": 15 if kind == "active" else 0,
                "avg_swimming_cadence": 20,
            }
        )
        if bpm is not None:
            records.extend(
                {"timestamp": begin + timedelta(seconds=second), "heart_rate": bpm}
                for second in range(duration)
            )
    end = FIT_START + timedelta(seconds=410)
    lap = {
        "timestamp": end,
        "start_time": FIT_START,
        "total_elapsed_time": 410.0,
        "total_timer_time": 410.0,
    }
    session = {**lap, "sport": "swimming", "sub_sport": "lap_swimming"}
    return build_activity_fit(sessions=[session], laps=[lap], lengths=lengths, records=records)


@pytest.fixture
def processed(conn: sqlite3.Connection, clock: FixedClock) -> sqlite3.Connection:
    seed_activity(
        conn,
        clock,
        AID,
        summary=summary_copy("pool_swim-1", AID),
        original_file=build_zip({f"{AID}_ACTIVITY.fit": _fit()}),
    )
    process_activity(conn, AID, clock)
    return conn


def test_thresholds() -> None:
    assert MIN_IDLE_S == 60
    assert HR_RATIO == 0.90


def test_idle_flags(processed: sqlite3.Connection) -> None:
    rows = processed.execute(
        "SELECT length_type, likely_unrecorded_swim FROM swim_lengths"
        " WHERE activity_id = ? ORDER BY length_index",
        (AID,),
    ).fetchall()
    assert [r[1] for r in rows if r[0] == "idle"] == [1, 0, 0, None]


def test_active_lengths_null(processed: sqlite3.Connection) -> None:
    flags = processed.execute(
        "SELECT likely_unrecorded_swim FROM swim_lengths"
        " WHERE activity_id = ? AND length_type = 'active'",
        (AID,),
    ).fetchall()
    assert len(flags) == 4
    assert all(f[0] is None for f in flags)


def test_metrics_sums(processed: sqlite3.Connection) -> None:
    probable, unrecorded, recorded = processed.execute(
        "SELECT probable_rest_duration_s, likely_unrecorded_swim_duration_s,"
        " recorded_rest_duration_s FROM swimming_metrics WHERE activity_id = ?",
        (AID,),
    ).fetchone()
    assert unrecorded == pytest.approx(90.0)
    assert probable == pytest.approx(90.0 + 20.0 + 90.0)
    assert recorded == pytest.approx(290.0)  # device view, timer seconds, unchanged


def test_views_expose_columns(processed: sqlite3.Connection) -> None:
    swimming = processed.execute(
        "SELECT probable_rest_duration_s, likely_unrecorded_swim_duration_s"
        " FROM v_swimming WHERE activity_id = ?",
        (AID,),
    ).fetchone()
    assert swimming == (200.0, 90.0)
    flags = processed.execute(
        "SELECT likely_unrecorded_swim FROM v_swim_lengths WHERE activity_id = ?"
        " AND length_type = 'idle' ORDER BY length_index",
        (AID,),
    ).fetchall()
    assert [f[0] for f in flags] == [1, 0, 0, None]
