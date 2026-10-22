"""Build ``swim_lengths`` rows from decoded FIT ``length`` messages.

Pure functions: no I/O, no clock, no host timezone. Provenance columns (``activity_id``,
``processing_run_id``, ``source_payload_id``) are added by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
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
