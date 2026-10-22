"""Unit tests for ``swim_length_rows`` and FIT length decoding."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from garmin_archive.processing.fitfile import decode_fit
from garmin_archive.processing.swim import swim_length_rows
from helpers.fitbuilder import FIT_START, build_activity_fit, running_fit

COLUMNS = {
    "length_index",
    "lap_index",
    "start_at_utc",
    "length_type",
    "stroke_type",
    "distance_m",
    "timer_duration_s",
    "elapsed_duration_s",
    "stroke_count",
    "avg_stroke_rate_spm",
    "swolf",
}


def _msg(offset: float, kind: str = "active", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "start_time": FIT_START + timedelta(seconds=offset),
        "length_type": kind,
        "swim_stroke": "freestyle",
        "total_timer_time": 20.0,
        "total_elapsed_time": 21.0,
        "total_strokes": 15,
        "avg_swimming_cadence": 22,
    }
    return base | fields


def _lap(offset: float, length: float) -> dict[str, Any]:
    return {
        "start_time": FIT_START + timedelta(seconds=offset),
        "total_elapsed_time": length,
        "total_timer_time": length,
    }


def test_active_row_values_and_keys() -> None:
    [row] = swim_length_rows([_msg(0)], [_lap(0, 100)], 25.0)
    assert set(row) == COLUMNS
    assert row["length_index"] == 0
    assert row["distance_m"] == 25.0
    assert row["swolf"] == pytest.approx(35.0)
    assert row["stroke_count"] == 15
    assert row["avg_stroke_rate_spm"] == 22
    assert row["timer_duration_s"] == 20.0
    assert row["elapsed_duration_s"] == 21.0
    assert row["start_at_utc"] == "2026-06-25T06:42:28Z"
    assert row["stroke_type"] == "freestyle"


def test_idle_length_has_null_distance_and_swolf() -> None:
    [row] = swim_length_rows([_msg(0, "idle")], [], 25.0)
    assert row["length_type"] == "idle"
    assert row["distance_m"] is None
    assert row["swolf"] is None


@pytest.mark.parametrize("strokes", [None, 0])
def test_drill_without_strokes_has_null_swolf(strokes: int | None) -> None:
    message = _msg(0, swim_stroke="drill")
    if strokes is None:
        del message["total_strokes"]
    else:
        message["total_strokes"] = strokes
    [row] = swim_length_rows([message], [], 25.0)
    assert row["swolf"] is None
    assert row["distance_m"] == 25.0


def test_length_index_follows_message_order() -> None:
    rows = swim_length_rows([_msg(40), _msg(0), _msg(80)], [], 25.0)
    assert [r["length_index"] for r in rows] == [0, 1, 2]
    assert rows[0]["start_at_utc"] == "2026-06-25T06:43:08Z"


def test_lap_index_by_time_window() -> None:
    laps = [_lap(0, 100), _lap(100, 100)]
    rows = swim_length_rows([_msg(10), _msg(110), _msg(500)], laps, 25.0)
    assert [r["lap_index"] for r in rows] == [0, 1, None]


def test_empty_laps_give_null_lap_index() -> None:
    [row] = swim_length_rows([_msg(10)], [], 25.0)
    assert row["lap_index"] is None


def test_unknown_pool_length_gives_null_distance() -> None:
    [row] = swim_length_rows([_msg(0)], [], None)
    assert row["distance_m"] is None
    assert row["swolf"] == pytest.approx(35.0)


def test_no_lengths_gives_no_rows() -> None:
    assert swim_length_rows([], [_lap(0, 10)], 25.0) == []


# ---- decode_fit lengths -----------------------------------------------------------------


def test_decode_fit_lengths_empty_without_length_messages() -> None:
    assert decode_fit(running_fit()).lengths == []


def test_decode_fit_returns_pool_swim_lengths() -> None:
    lengths = [
        _msg(0) | {"timestamp": FIT_START + timedelta(seconds=20)},
        _msg(20, "idle") | {"timestamp": FIT_START + timedelta(seconds=40), "total_strokes": 0},
    ]
    decoded = decode_fit(build_activity_fit(lengths=lengths)).lengths
    assert [m["length_type"] for m in decoded] == ["active", "idle"]
    assert decoded[0]["swim_stroke"] == "freestyle"
    assert decoded[0]["total_timer_time"] == pytest.approx(20.0)
    assert decoded[0]["total_strokes"] == 15
