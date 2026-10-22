"""Deterministic sample selection for the contract discovery tool (spec B4). Pure; no I/O."""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_PER_CATEGORY = 2
DEFAULT_CARDIO_TYPES: tuple[str, ...] = (
    "cardio",
    "indoor_cardio",
    "hiit",
    "stair_climbing",
    "elliptical",
    "strength_training",
)
CATEGORY_ORDER: tuple[str, ...] = (
    "explicit",
    "multisport",
    "manual",
    "pool_swim",
    "open_water_swim",
    "running",
    "cardio",
    "other",
)
NOT_OBSERVED_NOTE = "not present in account"
STATUS_CONFIRMED = "confirmed"
STATUS_NOT_OBSERVED = "not_observed"

Item = Mapping[str, Any]


@dataclass(frozen=True)
class SelectedSample:
    """One activity chosen for fetching. `item` is None for explicit IDs not in the inventory."""

    name: str
    category: str
    activity_id: str
    type_key: str | None
    item: Item | None
    parent_name: str | None = None
    in_inventory: bool = True
    note: str | None = None


@dataclass(frozen=True)
class CategoryResult:
    status: str
    selected: int
    note: str | None = None


@dataclass(frozen=True)
class SelectionResult:
    samples: tuple[SelectedSample, ...]
    categories: Mapping[str, CategoryResult]
    other_type_key: str | None = None


def _activity_id(item: Item) -> str:
    return str(item.get("activityId", ""))


def _type_key(item: Item) -> str | None:
    activity_type = item.get("activityType")
    if isinstance(activity_type, Mapping):
        key = activity_type.get("typeKey")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(key, str):
            return key
    return None


def _start_local(item: Item) -> str:
    value = item.get("startTimeLocal")
    return value if isinstance(value, str) else ""


def _sort_key(item: Item) -> tuple[str, int, str]:
    activity_id = _activity_id(item)
    # Numeric IDs sort numerically; the string is the tie-break for non-numeric IDs.
    return (_start_local(item), int(activity_id) if activity_id.isdigit() else -1, activity_id)


def _is_parent(item: Item) -> bool:
    return item.get("isParent") is True or item.get("parent") is True


def _is_manual(item: Item) -> bool:
    return item.get("manualActivity") is True or item.get("isManualActivity") is True


def _is_child(item: Item) -> bool:
    return item.get("parentId") is not None


def select_samples(
    inventory: Iterable[Item],
    explicit_ids: Sequence[str] = (),
    per_category: int = DEFAULT_PER_CATEGORY,
    cardio_types: Iterable[str] = DEFAULT_CARDIO_TYPES,
) -> SelectionResult:
    """Select samples per spec B4. First matching category wins; output is deterministic."""
    cardio = frozenset(cardio_types)
    items = sorted(inventory, key=_sort_key)
    by_id: dict[str, Item] = {}
    for item in items:
        by_id.setdefault(_activity_id(item), item)

    samples: list[SelectedSample] = []
    claimed: set[str] = set()

    # 1. explicit (not counted toward N, de-duplicated, order preserved)
    explicit_count = 0
    for raw_id in explicit_ids:
        activity_id = str(raw_id)
        if activity_id in claimed:
            continue
        claimed.add(activity_id)
        explicit_count += 1
        item = by_id.get(activity_id)
        samples.append(
            SelectedSample(
                name=f"explicit-{explicit_count}",
                category="explicit",
                activity_id=activity_id,
                type_key=_type_key(item) if item is not None else None,
                item=item,
                in_inventory=item is not None,
                note=None if item is not None else "explicit ID absent from inventory",
            )
        )

    candidates = [i for i in items if _activity_id(i) not in claimed]
    children_by_parent: dict[str, list[Item]] = {}
    for item in candidates:
        if _is_child(item):
            children_by_parent.setdefault(str(item.get("parentId")), []).append(item)

    counts: dict[str, int] = {"explicit": explicit_count}

    def take(category: str, matches: list[Item]) -> None:
        chosen = matches[: max(per_category, 0)]
        for n, item in enumerate(chosen, start=1):
            samples.append(
                SelectedSample(
                    name=f"{category}-{n}",
                    category=category,
                    activity_id=_activity_id(item),
                    type_key=_type_key(item),
                    item=item,
                )
            )
            if category == "multisport":
                kids = children_by_parent.get(_activity_id(item), [])
                for k, child in enumerate(kids, start=1):
                    samples.append(
                        SelectedSample(
                            name=f"{category}-{n}-child-{k}",
                            category=category,
                            activity_id=_activity_id(child),
                            type_key=_type_key(child),
                            item=child,
                            parent_name=f"{category}-{n}",
                        )
                    )
        counts[category] = len(chosen)

    # Assign each non-child, non-explicit item to its first matching category.
    assigned: dict[str, list[Item]] = {c: [] for c in CATEGORY_ORDER}
    remaining: list[Item] = []
    for item in candidates:
        type_key = _type_key(item)
        if _is_parent(item):
            assigned["multisport"].append(item)
        elif _is_child(item):
            continue
        elif _is_manual(item):
            assigned["manual"].append(item)
        elif type_key == "lap_swimming":
            assigned["pool_swim"].append(item)
        elif type_key == "open_water_swimming":
            assigned["open_water_swim"].append(item)
        elif type_key is not None and "running" in type_key:
            assigned["running"].append(item)
        elif type_key in cardio:
            assigned["cardio"].append(item)
        else:
            remaining.append(item)

    other_type_key: str | None = None
    freq = Counter(k for k in (_type_key(i) for i in remaining) if k is not None)
    if freq:
        other_type_key = min(freq, key=lambda k: (-freq[k], k))
        assigned["other"] = [i for i in remaining if _type_key(i) == other_type_key]

    for category in CATEGORY_ORDER[1:]:
        take(category, assigned[category])

    categories = {
        category: (
            CategoryResult(STATUS_CONFIRMED, counts[category])
            if counts[category]
            else CategoryResult(STATUS_NOT_OBSERVED, 0, NOT_OBSERVED_NOTE)
        )
        for category in CATEGORY_ORDER
    }
    return SelectionResult(tuple(samples), categories, other_type_key)
