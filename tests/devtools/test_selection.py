from __future__ import annotations

import random
from typing import Any

from garmin_archive.devtools.discovery.selection import (
    CATEGORY_ORDER,
    NOT_OBSERVED_NOTE,
    select_samples,
)


def act(
    i: int, type_key: str = "walking", start: str | None = None, **extra: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "activityId": i,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start or f"2024-01-{i % 28 + 1:02d} 08:00:00",
    }
    item.update(extra)
    return item


def names(result: Any) -> list[str]:
    return [s.name for s in result.samples]


def test_multisport_parent_and_children_naming_and_order() -> None:
    inv = [
        act(1, "multi_sport", "2024-01-01 08:00:00", isParent=True),
        act(12, "swimming", "2024-01-01 08:30:00", parentId=1),
        act(11, "cycling", "2024-01-01 08:30:00", parentId=1),
        act(13, "running", "2024-01-01 08:00:00", parentId=1),
    ]
    result = select_samples(inv)
    assert names(result) == [
        "multisport-1",
        "multisport-1-child-1",
        "multisport-1-child-2",
        "multisport-1-child-3",
    ]
    assert [s.activity_id for s in result.samples] == ["1", "13", "11", "12"]
    assert result.samples[1].parent_name == "multisport-1"
    # Children are not selected into other categories.
    assert result.categories["running"].status == "not_observed"


def test_first_match_precedence() -> None:
    inv = [
        act(1, "running", manualActivity=True),
        act(2, "running", isParent=True),
        act(3, "running"),
    ]
    result = select_samples(inv)
    by_name = {s.name: s.activity_id for s in result.samples}
    assert by_name == {"multisport-1": "2", "manual-1": "1", "running-1": "3"}


def test_n_limit_children_not_counted() -> None:
    inv = [act(i, "running") for i in range(1, 6)]
    inv += [act(10 + i, "multi_sport", isParent=True) for i in range(3)]
    inv += [act(20 + i, "swimming", parentId=10) for i in range(3)]
    result = select_samples(inv, per_category=2)
    assert result.categories["running"].selected == 2
    assert result.categories["multisport"].selected == 2
    assert sum(1 for s in result.samples if s.category == "multisport") == 2 + 3
    assert sum(1 for s in result.samples if s.category == "running") == 2


def test_deterministic_under_shuffle() -> None:
    inv = [act(i, ("running", "lap_swimming", "cardio", "walking")[i % 4]) for i in range(1, 30)]
    baseline = select_samples(inv)
    for seed in range(5):
        shuffled = inv[:]
        random.Random(seed).shuffle(shuffled)
        again = select_samples(shuffled)
        assert again.samples == baseline.samples
        assert again.categories == baseline.categories


def test_ordering_by_start_then_numeric_id() -> None:
    inv = [
        act(100, "running", "2024-02-01 08:00:00"),
        act(9, "running", "2024-02-01 08:00:00"),
        act(50, "running", "2024-01-01 08:00:00"),
    ]
    result = select_samples(inv, per_category=3)
    assert [s.activity_id for s in result.samples] == ["50", "9", "100"]


def test_explicit_ids_present_and_absent_not_counted() -> None:
    inv = [act(1, "running"), act(2, "running"), act(3, "running")]
    result = select_samples(inv, explicit_ids=["2", "999", "2"], per_category=2)
    assert names(result)[:2] == ["explicit-1", "explicit-2"]
    present, absent = result.samples[0], result.samples[1]
    assert present.in_inventory and present.type_key == "running"
    assert not absent.in_inventory and absent.item is None and absent.note
    assert result.categories["explicit"].selected == 2
    running = [s.activity_id for s in result.samples if s.category == "running"]
    assert running == ["1", "3"]


def test_other_picks_most_frequent_then_alphabetical() -> None:
    inv = [act(1, "yoga"), act(2, "walking"), act(3, "walking"), act(4, "yoga"), act(5, "hiking")]
    result = select_samples(inv)
    assert result.other_type_key == "walking"
    assert [s.activity_id for s in result.samples if s.category == "other"] == ["2", "3"]
    tie = select_samples([act(1, "yoga"), act(2, "walking")])
    assert tie.other_type_key == "walking"


def test_empty_categories_not_observed_with_note() -> None:
    result = select_samples([act(1, "running")])
    assert list(result.categories) == list(CATEGORY_ORDER)
    pool = result.categories["pool_swim"]
    assert pool.status == "not_observed"
    assert pool.selected == 0
    assert pool.note == NOT_OBSERVED_NOTE
    assert result.categories["running"].status == "confirmed"
    assert result.other_type_key is None


def test_custom_cardio_types_and_default() -> None:
    inv = [act(1, "yoga"), act(2, "hiit")]
    default = select_samples(inv)
    assert [s.activity_id for s in default.samples if s.category == "cardio"] == ["2"]
    custom = select_samples(inv, cardio_types=["yoga"])
    assert [s.activity_id for s in custom.samples if s.category == "cardio"] == ["1"]
    assert custom.other_type_key == "hiit"


def test_swim_categories() -> None:
    inv = [act(1, "lap_swimming"), act(2, "open_water_swimming")]
    result = select_samples(inv)
    by_cat = {s.category: s.activity_id for s in result.samples}
    assert by_cat == {"pool_swim": "1", "open_water_swim": "2"}


def test_per_category_zero() -> None:
    inv = [act(1, "running"), act(2, "multi_sport", isParent=True), act(3, "x", parentId=2)]
    result = select_samples(inv, per_category=0)
    assert result.samples == ()
    assert all(c.status == "not_observed" for k, c in result.categories.items() if k != "explicit")
