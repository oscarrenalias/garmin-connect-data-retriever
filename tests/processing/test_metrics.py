"""Unit tests for the pure sport metric builders (no database)."""

from __future__ import annotations

from typing import Any

import pytest

from garmin_archive.processing.metrics import (
    cardio_metrics_row,
    cycling_metrics_row,
    running_metrics_row,
    swim_pool_length_m,
    swimming_metrics_row,
)
from helpers.archive_seed import load_fixture


def _summary(name: str) -> dict[str, Any]:
    return load_fixture("summaries", name)


def _dto(**fields: Any) -> dict[str, Any]:
    return {"summaryDTO": fields}


def _lengths(*items: tuple[str, float | None]) -> list[dict[str, Any]]:
    return [{"length_type": kind, "timer_duration_s": timer} for kind, timer in items]


# ---- running / cycling ------------------------------------------------------------------


def test_running_metrics_from_fixture() -> None:
    row = running_metrics_row(_summary("running-1"))
    assert row["avg_cadence_spm"] == pytest.approx(148.25)
    assert row["avg_stride_length_m"] == pytest.approx(0.8859, abs=1e-4)
    assert row["avg_ground_contact_time_ms"] == pytest.approx(306.9, abs=0.01)
    assert row["avg_vertical_ratio_pct"] == pytest.approx(8.99, abs=0.01)


@pytest.mark.parametrize("summary", [{}, {"summaryDTO": None}, {"summaryDTO": [1]}])
def test_metrics_without_usable_dto_are_all_none(summary: dict[str, Any]) -> None:
    assert set(running_metrics_row(summary).values()) == {None}
    assert set(cycling_metrics_row(summary).values()) == {None}


def test_cycling_without_power_or_cadence_is_all_none() -> None:
    row = cycling_metrics_row(_dto(distance=1000.0, duration=60.0))
    assert set(row.values()) == {None}
    assert len(row) == 7


def test_zero_is_kept_and_missing_is_none() -> None:
    row = cycling_metrics_row(_dto(averagePower=0, maxPower=0.0))
    assert row["avg_power_w"] == 0
    assert row["max_power_w"] == 0
    assert row["normalized_power_w"] is None


def test_non_numeric_and_bool_values_are_ignored() -> None:
    row = running_metrics_row(_dto(averageRunCadence=True, maxRunCadence="180"))
    assert row["avg_cadence_spm"] is None
    assert row["max_cadence_spm"] is None


# ---- swimming ---------------------------------------------------------------------------


def _pool_summary(unit: Any, length: Any = 25.0) -> dict[str, Any]:
    fields: dict[str, Any] = {"poolLength": length}
    if unit is not ...:
        fields["unitOfPoolLength"] = unit
    return _dto(**fields)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [({"unitKey": "meter"}, 25.0), ({"unitKey": "yard"}, 22.86)],
)
def test_pool_length_units(unit: dict[str, str], expected: float) -> None:
    warnings: list[str] = []
    assert swim_pool_length_m(_pool_summary(unit), warnings) == pytest.approx(expected)
    assert warnings == []


@pytest.mark.parametrize("unit", [{"unitKey": "furlong"}, {}, None, "meter", ...])
def test_unknown_or_missing_pool_unit_is_null_with_warning(unit: Any) -> None:
    warnings: list[str] = []
    assert swim_pool_length_m(_pool_summary(unit), warnings) is None
    assert len(warnings) == 1
    assert warnings[0].startswith("unknown_pool_length_unit")


def test_absent_pool_length_is_silent_null() -> None:
    warnings: list[str] = []
    assert swim_pool_length_m(_dto(), warnings) is None
    assert warnings == []


def test_pool_swim_metrics_from_fixture_and_lengths() -> None:
    warnings: list[str] = []
    lengths = _lengths(("active", 20.0), ("idle", 10.0), ("active", 25.5), ("idle", 5.0))
    row = swimming_metrics_row("lap_swimming", _summary("pool_swim-1"), lengths, warnings)
    assert row["swim_environment"] == "pool"
    assert row["pool_length_m"] == 25.0
    assert row["active_lengths"] == 55
    assert row["total_strokes"] == 510
    assert row["avg_swolf"] == 39.0
    assert row["active_swim_duration_s"] == pytest.approx(45.5)
    assert row["recorded_rest_duration_s"] == pytest.approx(15.0)
    assert row["length_coverage_status"] == "partial"
    assert warnings == []


def _pool_lengths(count: int, pool_m: float = 25.0) -> list[dict[str, Any]]:
    return [
        {"length_type": "active", "timer_duration_s": 20.0, "distance_m": pool_m}
        for _ in range(count)
    ]


def _swim_summary(distance: float, active_lengths: int) -> dict[str, Any]:
    return _dto(
        distance=distance,
        numberOfActiveLengths=active_lengths,
        poolLength=25.0,
        unitOfPoolLength={"unitKey": "meter"},
    )


def test_consistent_swim_is_complete() -> None:
    row = swimming_metrics_row("lap_swimming", _swim_summary(1175.0, 47), _pool_lengths(47), [])
    assert row["length_coverage_status"] == "complete"


@pytest.mark.parametrize("fit_count", [47, 58])
def test_edited_summary_gives_partial_in_both_directions(fit_count: int) -> None:
    summary = _swim_summary(
        58 * 25.0 if fit_count == 47 else 47 * 25.0, 58 if fit_count == 47 else 47
    )
    row = swimming_metrics_row("lap_swimming", summary, _pool_lengths(fit_count), [])
    assert row["length_coverage_status"] == "partial"


def test_distance_mismatch_alone_gives_partial() -> None:
    row = swimming_metrics_row("lap_swimming", _swim_summary(1200.0, 47), _pool_lengths(47), [])
    assert row["length_coverage_status"] == "partial"


def test_distance_within_half_pool_length_is_complete() -> None:
    row = swimming_metrics_row("lap_swimming", _swim_summary(1185.0, 47), _pool_lengths(47), [])
    assert row["length_coverage_status"] == "complete"


def test_swim_without_lengths_is_none() -> None:
    row = swimming_metrics_row("lap_swimming", _swim_summary(1175.0, 47), [], [])
    assert row["length_coverage_status"] == "none"


def test_open_water_metrics() -> None:
    warnings: list[str] = []
    row = swimming_metrics_row("open_water_swimming", _summary("open_water_swim-1"), [], warnings)
    assert row["swim_environment"] == "open_water"
    assert row["pool_length_m"] is None
    assert row["length_coverage_status"] == "none"
    assert row["active_swim_duration_s"] is None
    assert row["recorded_rest_duration_s"] is None
    assert warnings == []


def test_unknown_type_key_gives_null_environment() -> None:
    row = swimming_metrics_row("swimming", _dto(poolLength=25.0), [], [])
    assert row["swim_environment"] is None
    assert row["pool_length_m"] is None


def test_idle_rows_with_none_timers_give_null_rest() -> None:
    row = swimming_metrics_row(
        "lap_swimming",
        _pool_summary({"unitKey": "meter"}),
        _lengths(("active", 20.0), ("idle", None)),
        [],
    )
    assert row["active_swim_duration_s"] == 20.0
    assert row["recorded_rest_duration_s"] is None


def test_no_idle_lengths_gives_zero_rest() -> None:
    row = swimming_metrics_row(
        "lap_swimming", _pool_summary({"unitKey": "meter"}), _lengths(("active", 20.0)), []
    )
    assert row["recorded_rest_duration_s"] == 0.0


def test_unit_warning_is_appended_by_swimming_row() -> None:
    warnings: list[str] = []
    swimming_metrics_row("lap_swimming", _pool_summary({"unitKey": "x"}), [], warnings)
    assert len(warnings) == 1


def test_bool_values_in_swim_summary_are_ignored() -> None:
    row = swimming_metrics_row(
        "lap_swimming",
        _dto(totalNumberOfStrokes=True, numberOfActiveLengths=False, averageSWOLF=True),
        [],
        [],
    )
    assert row["total_strokes"] is None
    assert row["active_lengths"] is None
    assert row["avg_swolf"] is None


# ---- cardio -----------------------------------------------------------------------------


def _sets(*items: tuple[str, float | None, int | None]) -> list[dict[str, Any]]:
    return [{"set_type": t, "duration_s": d, "repetitions": r} for t, d, r in items]


def test_cardio_summary_fields_from_fixture() -> None:
    row = cardio_metrics_row(_summary("cardio-1"), [])
    assert row["steps"] == 1184
    assert row["total_sets"] == 34
    assert row["active_sets"] == 17
    assert row["total_reps"] is None
    assert row["active_set_duration_s"] is None
    assert row["rest_set_duration_s"] is None


def test_cardio_set_sums_by_type() -> None:
    sets = _sets(("active", 30.0, 10), ("rest", 20.0, None), ("active", 45.0, 8), ("rest", 15.0, 0))
    row = cardio_metrics_row({}, sets)
    assert row["total_reps"] == 18
    assert row["active_set_duration_s"] == pytest.approx(75.0)
    assert row["rest_set_duration_s"] == pytest.approx(35.0)


def test_cardio_skips_none_values_and_unknown_types() -> None:
    sets = _sets(("active", None, None), ("active", 10.0, 5), ("warmup", 99.0, 99))
    row = cardio_metrics_row({}, sets)
    assert row["total_reps"] == 5
    assert row["active_set_duration_s"] == 10.0
    assert row["rest_set_duration_s"] is None


def test_cardio_active_sets_fall_back_to_row_count() -> None:
    sets = _sets(("active", 10.0, 5), ("rest", 5.0, None), ("active", 10.0, 5))
    assert cardio_metrics_row(_dto(steps=5), sets)["active_sets"] == 2


def test_cardio_empty_summary_and_sets_are_all_none() -> None:
    assert set(cardio_metrics_row({}, []).values()) == {None}
