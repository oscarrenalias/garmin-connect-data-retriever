"""Build running, swimming and cycling metric rows from archived summary JSON (SPEC §7.3b).

Pure functions: no I/O, no clock. Field names are limited to those present in the
OpenAPI ``SummaryDTO`` schema. Absent values stay ``None``; they are never coerced to 0.
Run-cadence values are already steps per minute and are not doubled.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast


def _dto(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    value = summary.get("summaryDTO")
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return {}


def _number(dto: Mapping[str, Any], key: str) -> float | int | None:
    value = dto.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _scaled(value: float | int | None, divisor: float) -> float | None:
    return None if value is None else value / divisor


def running_metrics_row(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``running_metrics`` metric columns (stride length converted cm -> m)."""
    dto = _dto(summary)
    return {
        "avg_cadence_spm": _number(dto, "averageRunCadence"),
        "max_cadence_spm": _number(dto, "maxRunCadence"),
        "avg_stride_length_m": _scaled(_number(dto, "strideLength"), 100),
        "avg_ground_contact_time_ms": _number(dto, "groundContactTime"),
        "avg_vertical_oscillation_cm": _number(dto, "verticalOscillation"),
        "avg_vertical_ratio_pct": _number(dto, "verticalRatio"),
        "avg_power_w": _number(dto, "averagePower"),
        "max_power_w": _number(dto, "maxPower"),
    }


def cycling_metrics_row(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``cycling_metrics`` metric columns."""
    dto = _dto(summary)
    return {
        "avg_cadence_rpm": _number(dto, "averageBikeCadence"),
        "max_cadence_rpm": _number(dto, "maxBikeCadence"),
        "avg_power_w": _number(dto, "averagePower"),
        "max_power_w": _number(dto, "maxPower"),
        "normalized_power_w": _number(dto, "normalizedPower"),
        "training_stress_score": _number(dto, "trainingStressScore"),
        "intensity_factor": _number(dto, "intensityFactor"),
    }


_YARD_M = 0.9144
_SWIM_ENVIRONMENTS = {"lap_swimming": "pool", "open_water_swimming": "open_water"}


class _Unset:
    """Sentinel type: argument not supplied."""


_UNSET = _Unset()


def swim_pool_length_m(summary: Mapping[str, Any], warnings: list[str]) -> float | None:
    """Pool length in metres from ``poolLength`` and ``unitOfPoolLength.unitKey``.

    Meters pass through, yards are multiplied by 0.9144. An unrecognised or missing unit
    gives ``None`` and appends a warning. An absent ``poolLength`` gives ``None`` silently.
    """
    dto = _dto(summary)
    length = _number(dto, "poolLength")
    if length is None:
        return None
    unit = dto.get("unitOfPoolLength")
    key: Any = cast("Mapping[str, Any]", unit).get("unitKey") if isinstance(unit, Mapping) else None
    if key == "meter":
        return float(length)
    if key == "yard":
        return float(length) * _YARD_M
    warnings.append(f"unknown_pool_length_unit: {key!r}")
    return None


def _length_time_s(lengths: Sequence[Mapping[str, Any]], length_type: str) -> float | None:
    """Sum of timer seconds over lengths of ``length_type``; ``None`` if none has a timer."""
    matching = [row for row in lengths if row.get("length_type") == length_type]
    if not matching:
        return 0.0
    values = [
        value
        for row in matching
        if not isinstance(value := row.get("timer_duration_s"), bool)
        and isinstance(value, int | float)
    ]
    return float(sum(values)) if values else None


def swimming_metrics_row(
    type_key: str,
    summary: Mapping[str, Any],
    lengths: Sequence[Mapping[str, Any]],
    warnings: list[str],
    pool_length_m: float | None | _Unset = _UNSET,
) -> dict[str, Any]:
    """Return ``swimming_metrics`` metric columns.

    Pass ``pool_length_m`` when the caller already derived it (and recorded any warning), so
    the pool-length warning is not appended twice. When omitted it is derived here.

    Summary metrics are copied as reported. ``lengths`` are ``swim_lengths`` rows. Active and
    rest durations come from length records only, never from elapsed-minus-timer.
    """
    dto = _dto(summary)
    environment = _SWIM_ENVIRONMENTS.get(type_key)
    has_lengths = bool(lengths)
    strokes = _number(dto, "totalNumberOfStrokes")
    active_lengths = _number(dto, "numberOfActiveLengths")
    if isinstance(pool_length_m, _Unset):
        pool_length_m = swim_pool_length_m(summary, warnings) if environment == "pool" else None
    return {
        "swim_environment": environment,
        "pool_length_m": pool_length_m,
        "active_lengths": None if active_lengths is None else int(active_lengths),
        "total_strokes": None if strokes is None else int(strokes),
        "avg_swolf": _number(dto, "averageSWOLF"),
        "avg_distance_per_stroke_m": _number(dto, "averageStrokeDistance"),
        "avg_stroke_rate_spm": _number(dto, "averageSwimCadence"),
        "active_swim_duration_s": _length_time_s(lengths, "active") if has_lengths else None,
        "recorded_rest_duration_s": _length_time_s(lengths, "idle") if has_lengths else None,
        "length_coverage_status": "complete" if has_lengths else "none",
    }


def _int(value: float | int | None) -> int | None:
    return None if value is None else int(value)


def _set_values(sets: Sequence[Mapping[str, Any]], set_type: str, column: str) -> list[float | int]:
    return [
        value
        for row in sets
        if row.get("set_type") == set_type
        and not isinstance(value := row.get(column), bool)
        and isinstance(value, int | float)
    ]


def cardio_metrics_row(
    summary: Mapping[str, Any], set_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Return ``cardio_metrics`` metric columns.

    ``steps``, ``total_sets`` and ``active_sets`` come from the summary (``active_sets`` falls
    back to the count of active ``activity_sets`` rows when the summary omits it). Reps and set
    durations are summed from ``set_rows`` by ``set_type``; a sum is ``None`` when no matching
    row carries a value. No intervals are derived from heart rate.
    """
    dto = _dto(summary)
    active_sets = _int(_number(dto, "activeSets"))
    if active_sets is None:
        active_count = sum(1 for row in set_rows if row.get("set_type") == "active")
        active_sets = active_count or None
    reps = _set_values(set_rows, "active", "repetitions")
    active_s = _set_values(set_rows, "active", "duration_s")
    rest_s = _set_values(set_rows, "rest", "duration_s")
    return {
        "steps": _int(_number(dto, "steps")),
        "total_sets": _int(_number(dto, "totalSets")),
        "active_sets": active_sets,
        "total_reps": int(sum(reps)) if reps else None,
        "active_set_duration_s": float(sum(active_s)) if active_s else None,
        "rest_set_duration_s": float(sum(rest_s)) if rest_s else None,
    }
