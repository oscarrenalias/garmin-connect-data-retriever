"""Build running and cycling metric rows from archived summary JSON (SPEC §7.3b).

Pure functions: no I/O, no clock. Field names are limited to those present in the
OpenAPI ``SummaryDTO`` schema. Absent values stay ``None``; they are never coerced to 0.
Run-cadence values are already steps per minute and are not doubled.
"""

from __future__ import annotations

from collections.abc import Mapping
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
