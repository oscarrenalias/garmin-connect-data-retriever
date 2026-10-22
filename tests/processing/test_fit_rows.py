from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from garmin_archive.processing.fit_rows import (
    lap_rows,
    sample_rows,
    session_rows,
    set_rows,
    to_utc_z,
)
from garmin_archive.processing.fitfile import decode_fit
from helpers.fitbuilder import FIT_START, cardio_fit, running_fit

T = FIT_START


def test_timestamps_end_in_z() -> None:
    assert to_utc_z(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05Z"
    assert to_utc_z(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05Z"
    plus2 = datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    assert to_utc_z(plus2) == "2026-01-02T03:04:05Z"
    assert to_utc_z(datetime(2026, 1, 2, 3, 4, 5, 500000, tzinfo=UTC)) == "2026-01-02T03:04:05.5Z"
    assert to_utc_z(None) is None
    assert to_utc_z("2026-01-02") is None


def test_running_session_and_laps() -> None:
    messages = decode_fit(running_fit())
    sessions = session_rows(messages.sessions)
    laps = lap_rows(messages.laps, messages.sessions)
    assert len(sessions) == 1
    assert sessions[0]["start_at_utc"] == "2026-06-25T06:42:28Z"
    assert sessions[0]["fit_sport"] == "running"
    assert sessions[0]["distance_m"] == pytest.approx(250.0, abs=0.05)
    assert sessions[0]["avg_hr_bpm"] == 130
    assert len(laps) == 3
    assert [r["lap_index"] for r in laps] == [0, 1, 2]
    assert {r["session_index"] for r in laps} == {0}
    assert all(r["start_at_utc"].endswith("Z") for r in laps)
    assert laps[0]["avg_speed_mps"] == pytest.approx(2.5, abs=0.01)
    assert laps[0]["lap_trigger"] == "manual"


def test_missing_fields_none_and_zero_preserved() -> None:
    sessions = session_rows([{"total_distance": 0.0, "avg_heart_rate": 0}])
    assert sessions[0]["distance_m"] == 0.0
    assert sessions[0]["avg_hr_bpm"] == 0
    assert sessions[0]["max_hr_bpm"] is None
    assert sessions[0]["start_at_utc"] is None
    assert sessions[0]["fit_sport"] is None


def _span(start: datetime, seconds: float) -> dict[str, Any]:
    return {"start_time": start, "total_elapsed_time": seconds}


def test_laps_assigned_to_matching_session_in_multi_session_file() -> None:
    sessions = [_span(T, 100), _span(T + timedelta(seconds=100), 100)]
    laps = [
        _span(T, 50),
        _span(T + timedelta(seconds=50), 50),
        _span(T + timedelta(seconds=100), 50),
        _span(T + timedelta(seconds=150), 50),
    ]
    assert [r["session_index"] for r in lap_rows(laps, sessions)] == [0, 0, 1, 1]


def test_lap_without_start_time_gets_session_zero() -> None:
    assert lap_rows([{"total_elapsed_time": 5.0}], [_span(T, 10)])[0]["session_index"] == 0


def test_empty_sessions_with_laps_gives_session_zero() -> None:
    assert lap_rows([_span(T, 5)], [])[0]["session_index"] == 0


def test_enhanced_avg_speed_preferred_over_avg_speed() -> None:
    rows = lap_rows([{"enhanced_avg_speed": 3.0, "avg_speed": 1.0}], [])
    assert rows[0]["avg_speed_mps"] == 3.0
    assert lap_rows([{"avg_speed": 1.5}], [])[0]["avg_speed_mps"] == 1.5
    assert lap_rows([{}], [])[0]["avg_speed_mps"] is None


def test_samples_100_records_with_gps_and_nulls() -> None:
    messages = decode_fit(running_fit())
    rows = sample_rows(messages.records)
    assert len(rows) == 100
    for i, row in enumerate(rows):
        assert row["latitude_deg"] == pytest.approx(
            (400_000_000 + i * 1000) * 180 / 2**31, abs=1e-6
        )
        assert row["longitude_deg"] == pytest.approx(
            (-300_000_000 + i * 500) * 180 / 2**31, abs=1e-6
        )
    assert rows[0]["elapsed_s"] == 0.0
    assert rows[99]["elapsed_s"] == pytest.approx(99.0)
    assert rows[0]["speed_mps"] == pytest.approx(2.5, abs=0.01)
    assert rows[10]["altitude_m"] == pytest.approx(21.0, abs=0.3)
    assert all(r["power_w"] is None and r["temperature_c"] is None for r in rows)
    assert all(r["cadence"] is None and r["cadence_unit"] is None for r in rows)


def test_cadence_unit_only_when_cadence_present() -> None:
    rows = sample_rows([{"cadence": 80}, {"heart_rate": 100}])
    assert (rows[0]["cadence"], rows[0]["cadence_unit"]) == (80, "fit_rpm")
    assert (rows[1]["cadence"], rows[1]["cadence_unit"]) == (None, None)


def test_enhanced_fields_take_precedence() -> None:
    row = sample_rows(
        [{"enhanced_speed": 3.0, "speed": 1.0, "enhanced_altitude": 50.0, "altitude": 10.0}]
    )[0]
    assert row["speed_mps"] == 3.0
    assert row["altitude_m"] == 50.0
    plain = sample_rows([{"speed": 1.0, "altitude": 10.0}])[0]
    assert (plain["speed_mps"], plain["altitude_m"]) == (1.0, 10.0)


def test_zero_lat_long_stay_zero_and_missing_timestamp_gives_none_elapsed() -> None:
    rows = sample_rows(
        [{"timestamp": T, "position_lat": 0, "position_long": 0}, {"heart_rate": 90}]
    )
    assert rows[0]["latitude_deg"] == 0.0
    assert rows[0]["longitude_deg"] == 0.0
    assert rows[1]["elapsed_s"] is None
    assert rows[1]["timestamp_utc"] is None
    assert rows[1]["latitude_deg"] is None


def test_sets_ten_rows_and_category_forms() -> None:
    messages = decode_fit(cardio_fit())
    rows = set_rows(messages.sets)
    assert len(rows) == 10
    assert rows[0]["set_type"] == "active"
    assert rows[1]["set_type"] == "rest"
    assert rows[0]["duration_s"] == pytest.approx(30.0)
    assert rows[3]["repetitions"] == 13
    assert rows[0]["weight_kg"] is None
    assert rows[0]["start_at_utc"] == "2026-06-25T06:42:28Z"


def test_set_category_list_scalar_and_empty() -> None:
    rows = set_rows([{"category": ["push_up", "x"]}, {"category": "curl"}, {"category": []}, {}])
    assert [r["category"] for r in rows] == ["push_up", "curl", None, None]


def test_running_cadence_is_doubled_with_fractional_part() -> None:
    row = sample_rows([{"cadence": 74, "fractional_cadence": 0.125}], running=True)[0]
    assert (row["cadence"], row["cadence_unit"]) == (pytest.approx(148.25), "spm")


def test_running_cadence_without_fractional_part() -> None:
    row = sample_rows([{"cadence": 74}], running=True)[0]
    assert (row["cadence"], row["cadence_unit"]) == (148.0, "spm")


def test_non_running_cadence_stays_raw_and_ignores_fractional() -> None:
    row = sample_rows([{"cadence": 74, "fractional_cadence": 0.125}])[0]
    assert (row["cadence"], row["cadence_unit"]) == (74, "fit_rpm")


@pytest.mark.parametrize("running", [True, False])
def test_missing_cadence_gives_null_cadence_and_unit(running: bool) -> None:
    row = sample_rows([{"heart_rate": 100}], running=running)[0]
    assert (row["cadence"], row["cadence_unit"]) == (None, None)
