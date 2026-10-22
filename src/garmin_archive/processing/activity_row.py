"""Build the ``activities`` row from archived summary JSON (SPEC §7.2, §7.3).

Pure functions: no I/O, no clock, no host timezone. The summary JSON is the primary
source; the activity-search item fills only fields the summary lacks. Absent values
stay ``None``; they are never coerced to 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypeVar, cast

from garmin_archive.processing.sports import sport_family

_T = TypeVar("_T")

# (row column, summaryDTO key, search item key)
_NUMERIC_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("distance_m", "distance", "distance"),
    ("timer_duration_s", "duration", "duration"),
    ("elapsed_duration_s", "elapsedDuration", "elapsedDuration"),
    ("moving_duration_s", "movingDuration", "movingDuration"),
    ("avg_speed_mps", "averageSpeed", "averageSpeed"),
    ("max_speed_mps", "maxSpeed", "maxSpeed"),
    ("avg_hr_bpm", "averageHR", "averageHR"),
    ("max_hr_bpm", "maxHR", "maxHR"),
    ("calories_kcal", "calories", "calories"),
    ("elevation_gain_m", "elevationGain", "elevationGain"),
    ("elevation_loss_m", "elevationLoss", "elevationLoss"),
    ("aerobic_training_effect", "trainingEffect", "aerobicTrainingEffect"),
    ("anaerobic_training_effect", "anaerobicTrainingEffect", "anaerobicTrainingEffect"),
    ("training_load", "activityTrainingLoad", "activityTrainingLoad"),
)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return {}


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | str):
        text = str(value)
        return text or None
    return None


def _parse_wall_time(value: Any) -> datetime | None:
    """Parse an offset-free Garmin timestamp (``T`` or space separated)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed


def _format(parsed: datetime) -> str:
    text = parsed.isoformat(timespec="seconds" if parsed.microsecond == 0 else "microseconds")
    return text


def _first(*values: _T | None) -> _T | None:
    for value in values:
        if value is not None:
            return value
    return None


def build_activity_row(
    summary: Mapping[str, Any],
    search_item: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the ``activities`` columns derivable from ``summary`` and ``search_item``.

    ``processing_status`` and ``processed_at_utc`` are owned by the processing run and
    are not set here. Raises ``ValueError`` if no activity ID is available.
    """
    item = _mapping(search_item)
    dto = _mapping(summary.get("summaryDTO"))
    type_dto = _mapping(summary.get("activityTypeDTO"))
    item_type = _mapping(item.get("activityType"))
    tz_dto = _mapping(summary.get("timeZoneUnitDTO"))

    activity_id = _first(_id(summary.get("activityId")), _id(item.get("activityId")))
    if activity_id is None:
        raise ValueError("activity ID missing from summary and search item")

    type_key = _first(_text(type_dto.get("typeKey")), _text(item_type.get("typeKey")))

    gmt = _parse_wall_time(dto.get("startTimeGMT")) or _parse_wall_time(item.get("startTimeGMT"))
    local = _parse_wall_time(dto.get("startTimeLocal")) or _parse_wall_time(
        item.get("startTimeLocal")
    )
    start_local = _format(local) if local is not None else None

    multisport = summary.get("isMultiSportParent")
    if not isinstance(multisport, bool):
        multisport = item.get("isParent")
    if not isinstance(multisport, bool):
        multisport = None

    row: dict[str, Any] = {
        "activity_id": activity_id,
        "parent_activity_id": _id(summary.get("parentId")),
        "is_multisport_parent": multisport,
        "type_key": type_key,
        "sport_family": sport_family(type_key),
        "name": _first(_text(summary.get("activityName")), _text(item.get("activityName"))),
        "description": _first(_text(summary.get("description")), _text(item.get("description"))),
        "start_at_utc": _format(gmt) + "Z" if gmt is not None else None,
        "start_local": start_local,
        "local_date": start_local[:10] if start_local is not None else None,
        "timezone": _text(tz_dto.get("timeZone")),
    }
    for column, summary_key, item_key in _NUMERIC_FIELDS:
        row[column] = _first(_number(dto.get(summary_key)), _number(item.get(item_key)))
    return row
