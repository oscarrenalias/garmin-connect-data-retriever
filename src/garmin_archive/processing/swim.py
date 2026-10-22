"""Build ``swim_lengths`` rows from decoded FIT ``length`` messages.

Pure functions: no I/O, no clock, no host timezone. Provenance columns (``activity_id``,
``processing_run_id``, ``source_payload_id``) are added by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from garmin_archive.processing.fit_rows import number, text, to_utc_z, window


def _lap_index(start: Any, windows: list[tuple[datetime, datetime] | None]) -> int | None:
    """Index of the lap whose time window holds ``start``; ``None`` when none does."""
    if not isinstance(start, datetime):
        return None
    probe = window({"start_time": start})
    if probe is None:
        return None
    moment = probe[0]
    for index, span in enumerate(windows):
        if span is not None and span[0] <= moment < span[1]:
            return index
    return None


def swim_length_rows(
    lengths: Sequence[Mapping[str, Any]],
    laps: Sequence[Mapping[str, Any]],
    pool_length_m: float | None,
) -> list[dict[str, Any]]:
    """One row per FIT length message (idle included), ``length_index`` in message order.

    ``distance_m`` is the pool length for active lengths and NULL for idle ones. ``swolf``
    is timer seconds plus strokes, only for active lengths with a valid timer time and a
    positive stroke count. ``lap_index`` is matched by time window and NULL if unmatched.
    """
    windows = [window(message) for message in laps]
    rows: list[dict[str, Any]] = []
    for index, message in enumerate(lengths):
        length_type = text(message.get("length_type"))
        active = length_type == "active"
        timer = number(message.get("total_timer_time"))
        strokes = number(message.get("total_strokes"))
        swolf = (
            float(timer + strokes)
            if active and timer is not None and timer > 0 and strokes is not None and strokes > 0
            else None
        )
        rows.append(
            {
                "length_index": index,
                "lap_index": _lap_index(message.get("start_time"), windows),
                "start_at_utc": to_utc_z(message.get("start_time")),
                "length_type": length_type,
                "stroke_type": text(message.get("swim_stroke")),
                "distance_m": pool_length_m if active else None,
                "timer_duration_s": timer,
                "elapsed_duration_s": number(message.get("total_elapsed_time")),
                "stroke_count": None if strokes is None else int(strokes),
                "avg_stroke_rate_spm": number(message.get("avg_swimming_cadence")),
                "swolf": swolf,
            }
        )
    return rows


MIN_IDLE_S = 60
HR_RATIO = 0.90


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _mean_hr(
    samples: Sequence[tuple[datetime, float]], start: datetime, seconds: float
) -> float | None:
    end = start + timedelta(seconds=seconds)
    values = [hr for moment, hr in samples if start <= moment < end]
    return sum(values) / len(values) if values else None


def flag_unrecorded_swim(
    lengths: Sequence[dict[str, Any]], samples: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Add ``likely_unrecorded_swim`` to every length row (heuristic, see SPEC notes).

    Only idle lengths are flagged: 1 when idle for at least ``MIN_IDLE_S`` with mean HR at
    least ``HR_RATIO`` times the duration-weighted mean HR of the active lengths, 0 when
    that is measurable but not met, NULL when the idle length or the activity has no HR.
    """
    hr_samples: list[tuple[datetime, float]] = []
    for sample in samples:
        moment = _utc(sample.get("timestamp_utc"))
        hr = number(sample.get("hr_bpm"))
        if moment is not None and hr is not None:
            hr_samples.append((moment, float(hr)))

    def span(row: Mapping[str, Any]) -> tuple[datetime, float] | None:
        start = _utc(row.get("start_at_utc"))
        elapsed = number(row.get("elapsed_duration_s"))
        return None if start is None or elapsed is None or elapsed <= 0 else (start, elapsed)

    weighted = 0.0
    weight = 0.0
    for row in lengths:
        if row.get("length_type") != "active" or (window_ := span(row)) is None:
            continue
        mean = _mean_hr(hr_samples, *window_)
        if mean is not None:
            weighted += mean * window_[1]
            weight += window_[1]
    reference = weighted / weight if weight else None

    result: list[dict[str, Any]] = []
    for row in lengths:
        flag: int | None = None
        if row.get("length_type") == "idle" and reference is not None:
            window_ = span(row)
            mean = None if window_ is None else _mean_hr(hr_samples, *window_)
            if window_ is not None and mean is not None:
                flag = int(window_[1] >= MIN_IDLE_S and mean >= HR_RATIO * reference)
        result.append({**row, "likely_unrecorded_swim": flag})
    return result
