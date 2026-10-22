"""Build ``activity_sessions`` and ``activity_laps`` rows from decoded FIT messages.

Pure functions: no I/O, no clock, no host timezone. Values are taken as the SDK decoded
them; absent or malformed fields stay ``None`` and are never coerced to 0. Provenance
columns (``activity_id``, ``processing_run_id``, ``source_payload_id``) are added by the
caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast


def to_utc_z(value: Any) -> str | None:
    """Format a FIT datetime as RFC 3339 UTC ending in ``Z`` (naive values are UTC)."""
    if not isinstance(value, datetime):
        return None
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}".rstrip("0")
    return text + "Z"


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int):
        return str(value)
    return None


def _first(message: Mapping[str, Any], *keys: str) -> float | int | None:
    for key in keys:
        number = _number(message.get(key))
        if number is not None:
            return number
    return None


def session_rows(sessions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per FIT session message, ``session_index`` following message order."""
    return [
        {
            "session_index": index,
            "fit_sport": _text(message.get("sport")),
            "fit_subsport": _text(message.get("sub_sport")),
            "start_at_utc": to_utc_z(message.get("start_time")),
            "distance_m": _number(message.get("total_distance")),
            "timer_duration_s": _number(message.get("total_timer_time")),
            "elapsed_duration_s": _number(message.get("total_elapsed_time")),
            "avg_hr_bpm": _number(message.get("avg_heart_rate")),
            "max_hr_bpm": _number(message.get("max_heart_rate")),
            "fit_message_index": index,
        }
        for index, message in enumerate(sessions)
    ]


def _window(message: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    start = message.get("start_time")
    if not isinstance(start, datetime):
        return None
    start_utc = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    elapsed = _number(message.get("total_elapsed_time")) or 0
    return start_utc, start_utc + (
        datetime.fromtimestamp(elapsed, UTC) - datetime.fromtimestamp(0, UTC)
    )


def _session_index(lap_start: Any, windows: list[tuple[datetime, datetime] | None]) -> int:
    """Index of the session whose window holds ``lap_start``; nearest earlier start otherwise."""
    if not windows or not isinstance(lap_start, datetime):
        return 0
    moment = (
        lap_start.replace(tzinfo=UTC) if lap_start.tzinfo is None else lap_start.astimezone(UTC)
    )
    chosen = 0
    for index, window in enumerate(windows):
        if window is None:
            continue
        start, end = window
        if start <= moment < end:
            return index
        if start <= moment:
            chosen = index
    return chosen


def lap_rows(
    laps: Sequence[Mapping[str, Any]], sessions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One row per FIT lap message; ``session_index`` assigned by lap start time."""
    windows = [_window(message) for message in sessions]
    return [
        {
            "lap_index": index,
            "session_index": _session_index(message.get("start_time"), windows),
            "start_at_utc": to_utc_z(message.get("start_time")),
            "distance_m": _number(message.get("total_distance")),
            "timer_duration_s": _number(message.get("total_timer_time")),
            "elapsed_duration_s": _number(message.get("total_elapsed_time")),
            "avg_hr_bpm": _number(message.get("avg_heart_rate")),
            "max_hr_bpm": _number(message.get("max_heart_rate")),
            "avg_speed_mps": _first(message, "enhanced_avg_speed", "avg_speed"),
            "lap_trigger": _text(message.get("lap_trigger")),
            "intensity": _text(message.get("intensity")),
        }
        for index, message in enumerate(laps)
    ]


_SEMICIRCLES_TO_DEG = 180 / 2**31


def _degrees(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else number * _SEMICIRCLES_TO_DEG


def _elapsed_s(moment: Any, origin: datetime | None) -> float | None:
    if origin is None or not isinstance(moment, datetime):
        return None
    current = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return (current - origin).total_seconds()


def sample_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per FIT record message at native rate; no resampling or gap filling.

    ``elapsed_s`` is measured from the first record carrying a timestamp. Cadence is the
    raw FIT value with ``cadence_unit`` ``fit_rpm`` and is only labelled when present.
    """
    origin: datetime | None = None
    for message in records:
        first = message.get("timestamp")
        if isinstance(first, datetime):
            origin = first.replace(tzinfo=UTC) if first.tzinfo is None else first.astimezone(UTC)
            break
    rows: list[dict[str, Any]] = []
    for index, message in enumerate(records):
        cadence = _number(message.get("cadence"))
        rows.append(
            {
                "sample_index": index,
                "timestamp_utc": to_utc_z(message.get("timestamp")),
                "elapsed_s": _elapsed_s(message.get("timestamp"), origin),
                "distance_m": _number(message.get("distance")),
                "speed_mps": _first(message, "enhanced_speed", "speed"),
                "hr_bpm": _number(message.get("heart_rate")),
                "cadence": cadence,
                "cadence_unit": None if cadence is None else "fit_rpm",
                "altitude_m": _first(message, "enhanced_altitude", "altitude"),
                "latitude_deg": _degrees(message.get("position_lat")),
                "longitude_deg": _degrees(message.get("position_long")),
                "power_w": _number(message.get("power")),
                "temperature_c": _number(message.get("temperature")),
            }
        )
    return rows


def _category(value: Any) -> str | None:
    item: Any = value
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        sequence = cast("Sequence[Any]", value)
        item = sequence[0] if sequence else None
    return _text(item)


def set_rows(sets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per FIT set message, ``set_index`` following message order."""
    return [
        {
            "set_index": index,
            "set_type": _text(message.get("set_type")),
            "start_at_utc": to_utc_z(message.get("start_time")),
            "duration_s": _number(message.get("duration")),
            "repetitions": _number(message.get("repetitions")),
            "weight_kg": _number(message.get("weight")),
            "category": _category(message.get("category")),
        }
        for index, message in enumerate(sets)
    ]
