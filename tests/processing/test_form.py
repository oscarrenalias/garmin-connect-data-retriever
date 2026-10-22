"""Unit tests for ``parse_form_fit`` (FORM goggles FIT -> form_sessions / form_lengths rows)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from garmin_fit_sdk import Encoder

from garmin_archive.processing.fitfile import FitUnavailableError
from garmin_archive.processing.form import parse_form_fit

START = datetime(2026, 6, 25, 6, 42, 28, tzinfo=UTC)
SERIAL = 987654321
SHA = "ab" * 32


def _length(offset: float, kind: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mesg_num": 101,
        "start_time": START + timedelta(seconds=offset),
        "length_type": kind,
        "total_timer_time": 20.0,
        "total_elapsed_time": 21.0,
    }
    if kind == "active":
        base |= {"swim_stroke": "freestyle", "total_strokes": 15}
    return base | fields


def _session(offset: float = 0, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mesg_num": 18,
        "start_time": START + timedelta(seconds=offset),
        "sport": "swimming",
        "sub_sport": "lap_swimming",
        "pool_length": 25.0,
        "pool_length_unit": "metric",
        "total_distance": 75.0,
        "total_timer_time": 100.0,
        "total_elapsed_time": 120.0,
        "avg_heart_rate": 130,
        "max_heart_rate": 150,
    }
    return base | fields


def _fit(sessions: Sequence[dict[str, Any]], lengths: Sequence[dict[str, Any]]) -> bytes:
    encoder = Encoder()
    encoder.write_mesg(
        {
            "mesg_num": 0,
            "type": "activity",
            "manufacturer": "form",
            "product": 0,
            "serial_number": SERIAL,
        }
    )
    for message in lengths:
        encoder.write_mesg(message)
    for message in sessions:
        encoder.write_mesg(message)
    encoder.write_mesg(
        {
            "mesg_num": 34,
            "timestamp": START,
            "local_timestamp": datetime(2026, 6, 25, 23, 30, 0, tzinfo=UTC),
        }
    )
    return encoder.close()


def test_active_idle_and_drill_lengths() -> None:
    lengths = [
        _length(0, "active"),
        _length(21, "active", swim_stroke="drill", total_strokes=10, total_timer_time=30.0),
        _length(51, "idle", total_elapsed_time=40.0),
        _length(91, "active", total_strokes=None),
    ]
    sessions, rows = parse_form_fit(_fit([_session()], lengths), 7, SHA)
    assert len(sessions) == 1
    session = sessions[0]
    assert session["form_session_id"] == "form:" + SHA[:16]
    assert session["source_payload_id"] == 7
    assert session["start_at_utc"] == "2026-06-25T06:42:28Z"
    assert session["local_date"] == "2026-06-25"
    assert session["environment"] == "pool"
    assert session["pool_length_m"] == 25.0
    assert session["distance_m"] == 75.0
    assert session["active_lengths"] == 3
    assert session["active_swim_duration_s"] == 70.0
    assert session["rest_duration_s"] == 40.0
    assert session["timer_duration_s"] == 100.0
    assert session["elapsed_duration_s"] == 120.0
    assert session["total_strokes"] == 25
    assert (session["avg_hr_bpm"], session["max_hr_bpm"]) == (130, 150)

    assert [row["length_type"] for row in rows] == ["active", "active", "idle", "active"]
    assert [row["distance_m"] for row in rows] == [25.0, 25.0, None, 25.0]
    assert rows[1]["stroke_type"] == "drill"
    assert rows[0]["swolf"] == 35.0
    assert rows[2]["swolf"] is None
    assert rows[3]["stroke_count"] is None and rows[3]["swolf"] is None
    assert all(row["form_session_id"] == session["form_session_id"] for row in rows)
    assert all(row["source_payload_id"] == 7 for row in rows)
    assert [row["length_index"] for row in rows] == [0, 1, 2, 3]


def test_yard_pool_length_converted() -> None:
    sessions, rows = parse_form_fit(
        _fit([_session(pool_length=25.0, pool_length_unit="statute")], [_length(0, "active")]),
        1,
        SHA,
    )
    assert sessions[0]["pool_length_m"] == pytest.approx(22.86)
    assert rows[0]["distance_m"] == pytest.approx(22.86)


def test_multi_session_ids_and_length_assignment() -> None:
    first = _session(0, total_elapsed_time=100.0)
    second = _session(200, total_elapsed_time=100.0)
    lengths = [_length(0, "active"), _length(30, "idle"), _length(210, "active")]
    sessions, rows = parse_form_fit(_fit([first, second], lengths), 3, SHA)
    ids = [session["form_session_id"] for session in sessions]
    assert ids == [f"form:{SHA[:16]}:0", f"form:{SHA[:16]}:1"]
    assert [(r["form_session_id"], r["length_index"]) for r in rows] == [
        (ids[0], 0),
        (ids[0], 1),
        (ids[1], 0),
    ]
    assert sessions[0]["active_lengths"] == 1
    assert sessions[1]["active_lengths"] == 1


def test_missing_values_stay_null_and_no_serial() -> None:
    bare = {"mesg_num": 18, "start_time": START}
    sessions, rows = parse_form_fit(_fit([bare], []), 1, SHA)
    session = sessions[0]
    assert rows == []
    for key in (
        "environment",
        "pool_length_m",
        "distance_m",
        "active_lengths",
        "active_swim_duration_s",
        "rest_duration_s",
        "timer_duration_s",
        "total_strokes",
        "avg_hr_bpm",
    ):
        assert session[key] is None


def test_no_serial_number_in_output() -> None:
    sessions, rows = parse_form_fit(_fit([_session()], [_length(0, "active")]), 1, SHA)
    for row in [*sessions, *rows]:
        assert "serial_number" not in row
        assert SERIAL not in row.values()
        assert str(SERIAL) not in repr(row)


def test_undecodable_data_raises() -> None:
    with pytest.raises(FitUnavailableError):
        parse_form_fit(b"not a fit file", 1, SHA)
