"""Build ``form_sessions`` and ``form_lengths`` rows from one FORM goggles FIT payload.

Pure functions: no I/O, no clock, no host timezone. Absent values stay ``None``. The device
serial number is never read. Matching columns (``matched_activity_id``, ``match_delta_s``,
``pool_length_mismatch``) are left to the matching pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from garmin_fit_sdk import Decoder, Stream

from garmin_archive.processing.fit_rows import number, session_index, text, to_utc_z, window
from garmin_archive.processing.fitfile import FitUnavailableError
from garmin_archive.processing.swim import swim_length_rows

_YARD_M = 0.9144
_ENVIRONMENTS = {"lap_swimming": "pool", "open_water": "open_water"}


def _pool_length_m(session: Mapping[str, Any]) -> float | None:
    """``pool_length`` in metres: ``statute`` means yards, ``metric`` (or no unit) metres."""
    length = number(session.get("pool_length"))
    if length is None:
        return None
    unit = text(session.get("pool_length_unit"))
    return float(length) * _YARD_M if unit == "statute" else float(length)


def _local_date(activity: Mapping[str, Any] | None) -> str | None:
    """Calendar date of ``local_timestamp``; the SDK value already reads as local wall time."""
    value = None if activity is None else activity.get("local_timestamp")
    return value.date().isoformat() if isinstance(value, datetime) else None


def _sum(rows: Sequence[Mapping[str, Any]], length_type: str, key: str) -> float | None:
    values = [
        row[key]
        for row in rows
        if row["length_type"] == length_type and isinstance(row[key], int | float)
    ]
    return float(sum(values)) if values else None


def parse_form_fit(
    data: bytes, payload_id: int, sha256: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(form_sessions rows, form_lengths rows)`` for one FORM FIT file.

    ``form_session_id`` is ``form:<sha256[:16]>``, with ``:<session index>`` appended when
    the file holds more than one session. Raises ``FitUnavailableError`` if undecodable.
    """
    try:
        decoder = Decoder(Stream.from_byte_array(bytearray(data)))
        raw, errors = decoder.read()
    except Exception as exc:
        raise FitUnavailableError("FIT file could not be decoded") from exc
    if errors:
        raise FitUnavailableError("FIT file could not be decoded")
    messages = cast("dict[str, list[dict[str, Any]]]", raw)
    sessions = messages.get("session_mesgs", [])
    laps = messages.get("lap_mesgs", [])
    lengths = messages.get("length_mesgs", [])
    activities = messages.get("activity_mesgs", [])
    local_date = _local_date(activities[0] if activities else None)

    windows = [window(message) for message in sessions]
    grouped: list[list[Mapping[str, Any]]] = [[] for _ in sessions]
    for message in lengths:
        index = session_index(message.get("start_time"), windows)
        grouped[index].append(message)

    base = f"form:{sha256[:16]}"
    session_rows: list[dict[str, Any]] = []
    length_rows: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        session_id = base if len(sessions) == 1 else f"{base}:{index}"
        pool = _pool_length_m(session)
        rows = swim_length_rows(grouped[index], laps, pool)
        active = [row for row in rows if row["length_type"] == "active"]
        strokes = [row["stroke_count"] for row in active if row["stroke_count"] is not None]
        active_lengths = number(session.get("num_active_lengths"))
        session_rows.append(
            {
                "form_session_id": session_id,
                "source_payload_id": payload_id,
                "session_index": index,
                "start_at_utc": to_utc_z(session.get("start_time")),
                "local_date": local_date,
                "environment": _ENVIRONMENTS.get(text(session.get("sub_sport")) or ""),
                "pool_length_m": pool,
                "distance_m": number(session.get("total_distance")),
                "active_lengths": len(active) if rows else _int(active_lengths),
                "active_swim_duration_s": _sum(rows, "active", "timer_duration_s"),
                "rest_duration_s": _sum(rows, "idle", "elapsed_duration_s"),
                "timer_duration_s": number(session.get("total_timer_time")),
                "elapsed_duration_s": number(session.get("total_elapsed_time")),
                "total_strokes": sum(strokes) if strokes else None,
                "avg_hr_bpm": number(session.get("avg_heart_rate")),
                "max_hr_bpm": number(session.get("max_heart_rate")),
            }
        )
        for row in rows:
            length_rows.append(
                {
                    "form_session_id": session_id,
                    "source_payload_id": payload_id,
                    **row,
                    "likely_unrecorded_swim": None,
                }
            )
    return session_rows, length_rows


def _int(value: float | int | None) -> int | None:
    return None if value is None else int(value)
