from __future__ import annotations

from typing import Any

import pytest

from garmin_archive.processing.activity_row import build_activity_row
from helpers.archive_seed import load_fixture

# fixture -> (type_key, family, start_utc, start_local, local_date, timezone)
EXPECTED = {
    "running-1": (
        "running", "running", "2026-06-25T06:42:28Z", "2026-06-25T09:42:28",
        "2026-06-25", "Europe/Athens",
    ),
    "pool_swim-1": (
        "lap_swimming", "swimming", "2026-03-29T07:51:47Z", "2026-03-29T10:51:47",
        "2026-03-29", "Europe/Athens",
    ),
    "open_water_swim-1": (
        "open_water_swimming", "swimming", "2026-07-05T05:45:15Z", "2026-07-05T07:45:15",
        "2026-07-05", "Europe/Paris",
    ),
    "cardio-1": (
        "indoor_cardio", "cardio", "2026-04-10T15:39:36Z", "2026-04-10T18:39:36",
        "2026-04-10", "Europe/Athens",
    ),
    "other-1": (
        "mobility", "other", "2026-04-10T16:20:40Z", "2026-04-10T19:20:40",
        "2026-04-10", "Europe/Athens",
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_rows(name: str) -> None:
    type_key, family, utc, local, local_date, tz = EXPECTED[name]
    row = build_activity_row(load_fixture("summaries", name), load_fixture("search-items", name))
    assert row["type_key"] == type_key
    assert row["sport_family"] == family
    assert row["start_at_utc"] == utc
    assert row["start_local"] == local
    assert row["local_date"] == local_date
    assert row["timezone"] == tz
    assert isinstance(row["activity_id"], str)


def test_running_numeric_values_from_summary() -> None:
    row = build_activity_row(load_fixture("summaries", "running-1"))
    assert row["distance_m"] == 5573.0
    assert row["timer_duration_s"] == pytest.approx(2516.779)
    assert row["avg_hr_bpm"] == 134.0
    assert row["calories_kcal"] == 490.0
    assert row["elevation_gain_m"] == 77.0


def test_search_item_fallback_when_summary_lacks_field() -> None:
    summary: dict[str, Any] = {"activityId": 1, "summaryDTO": {"distance": 10.0}}
    row = build_activity_row(summary, {"averageHR": 150.0, "distance": 99.0})
    assert row["avg_hr_bpm"] == 150.0
    assert row["distance_m"] == 10.0  # summary wins over a conflicting search item


def test_absent_metrics_are_none_and_zero_preserved() -> None:
    row = build_activity_row({"activityId": 1, "summaryDTO": {"distance": 0.0}})
    assert row["distance_m"] == 0.0
    for column in ("avg_hr_bpm", "calories_kcal", "training_load", "moving_duration_s"):
        assert row[column] is None
    assert row["start_at_utc"] is None
    assert row["local_date"] is None


def test_name_does_not_influence_family() -> None:
    summary = {
        "activityId": 1,
        "activityName": "Morning Run",
        "activityTypeDTO": {"typeKey": "cycling"},
    }
    row = build_activity_row(summary)
    assert row["sport_family"] == "cycling"
    assert row["name"] == "Morning Run"


def test_search_timestamp_with_space_separator() -> None:
    row = build_activity_row(
        {"activityId": 1},
        {"startTimeGMT": "2026-01-02 03:04:05", "startTimeLocal": "2026-01-02 05:04:05"},
    )
    assert row["start_at_utc"] == "2026-01-02T03:04:05Z"
    assert row["local_date"] == "2026-01-02"


@pytest.mark.parametrize("dto", [None, "oops", []])
def test_missing_or_invalid_summary_dto(dto: object) -> None:
    row = build_activity_row({"activityId": 7, "summaryDTO": dto})
    assert row["activity_id"] == "7"
    assert row["distance_m"] is None


def test_missing_activity_id_raises() -> None:
    with pytest.raises(ValueError):
        build_activity_row({}, {})


def test_parent_id_int_becomes_str() -> None:
    row = build_activity_row({"activityId": 2, "parentId": 12345, "isMultiSportParent": False})
    assert row["parent_activity_id"] == "12345"
    assert row["is_multisport_parent"] is False
