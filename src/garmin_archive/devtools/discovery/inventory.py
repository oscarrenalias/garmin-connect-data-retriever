"""Search pagination, page-size cap probes and inventory facts for the discovery tool (spec B3).

Raw bodies go to the `RawRecorder`. The returned facts hold only counts, booleans, allowlisted
headers and year-month strings; raw activity IDs never appear in them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from garmin_archive.devtools.discovery.credentials import SecretStr
from garmin_archive.devtools.discovery.http import DiscoveryHttp, HttpResponse, RetriesExhausted
from garmin_archive.devtools.discovery.raw import RawRecorder
from garmin_archive.devtools.discovery.selection import Item

SEARCH_PATH = "/activitylist-service/activities/search/activities"
DEFAULT_PAGE_SIZE = 99
DEFAULT_MAX_PAGES = 500
CAP_PROBE_LIMITS: tuple[int, ...] = (200, 1000)

_MONTH = re.compile(r"^(\d{4}-\d{2})")


@dataclass
class InventoryResult:
    """Outcome of the inventory step. `items` holds distinct items in first-seen order."""

    items: list[Item]
    inventory_status: str  # "complete" | "incomplete"
    partial: bool
    pagination: dict[str, Any]
    inventory: dict[str, Any]
    ordering_probe: dict[str, Any] | None = None
    cap_probes: list[dict[str, Any]] = field(default_factory=lambda: [])
    note: str | None = None


def _activity_id(item: Item) -> str:
    return str(item.get("activityId", ""))


def parse_items(body: bytes) -> list[Item] | None:
    """Extract activity items from a search response. None when the body is not recognisable."""
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None
    candidate: object = parsed
    if isinstance(parsed, dict):
        obj = cast("dict[str, object]", parsed)
        candidate = None
        for key in ("activityList", "activities", "items"):
            if isinstance(obj.get(key), list):
                candidate = obj[key]
                break
    if not isinstance(candidate, list):
        return None
    items: list[Item] = []
    for entry in cast("list[object]", candidate):
        if isinstance(entry, dict):
            items.append(cast("Item", entry))
    return items


def _fetch(
    http: DiscoveryHttp,
    recorder: RawRecorder,
    token: SecretStr,
    label: str,
    params: Mapping[str, str],
) -> tuple[HttpResponse | RetriesExhausted, list[Item] | None]:
    response = http.get(
        SEARCH_PATH,
        params=params,
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
    )
    if isinstance(response, RetriesExhausted):
        return response, None
    recorder.record(label, "GET", SEARCH_PATH, params, response)
    if not 200 <= response.status < 300:
        return response, None
    return response, parse_items(response.body)


def _year_month(item: Item) -> str | None:
    value = item.get("startTimeLocal")
    if isinstance(value, str):
        match = _MONTH.match(value)
        if match:
            return match.group(1)
    return None


def _type_info(item: Item) -> tuple[str | None, str | None]:
    activity_type = item.get("activityType")
    if not isinstance(activity_type, Mapping):
        return None, None
    fields = cast("Mapping[str, object]", activity_type)
    key = fields.get("typeKey")
    parent = fields.get("parentTypeId")
    return (
        key if isinstance(key, str) else None,
        str(parent) if parent is not None else None,
    )


def compute_facts(items: Sequence[Item]) -> dict[str, Any]:
    """Counts and booleans over distinct items. Never includes activity IDs."""
    by_type: Counter[str] = Counter()
    by_parent_type: Counter[str] = Counter()
    child = parent = manual = 0
    months: list[str] = []
    owners: set[str] = set()
    for item in items:
        type_key, parent_type_id = _type_info(item)
        if type_key is not None:
            by_type[type_key] += 1
        if parent_type_id is not None:
            by_parent_type[parent_type_id] += 1
        if item.get("parentId") is not None:
            child += 1
        if item.get("isParent") is True or item.get("parent") is True:
            parent += 1
        if item.get("manualActivity") is True or item.get("isManualActivity") is True:
            manual += 1
        month = _year_month(item)
        if month is not None:
            months.append(month)
        owner = item.get("ownerId")
        if owner is not None:
            owners.add(str(owner))
    return {
        "by_type_key": dict(sorted(by_type.items())),
        "by_parent_type_id": dict(sorted(by_parent_type.items())),
        "child_count": child,
        "parent_count": parent,
        "manual_count": manual,
        "oldest_month": min(months) if months else None,
        "newest_month": max(months) if months else None,
        "owner_id_constant": len(owners) <= 1,
    }


def _same_start_time_count(all_items: Sequence[Item]) -> int:
    """Items (by distinct ID) sharing a `startTimeLocal` with at least one other distinct item."""
    by_start: dict[str, set[str]] = {}
    for item in all_items:
        start = item.get("startTimeLocal")
        if isinstance(start, str):
            by_start.setdefault(start, set()).add(_activity_id(item))
    return sum(len(ids) for ids in by_start.values() if len(ids) > 1)


def compare_first_page(first: Sequence[str], again: Sequence[str]) -> dict[str, Any]:
    differing = sum(1 for a, b in zip(first, again, strict=False) if a != b)
    differing += abs(len(first) - len(again))
    return {"stable": differing == 0, "differing_positions": differing}


def run_inventory(
    http: DiscoveryHttp,
    recorder: RawRecorder,
    token: SecretStr,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> InventoryResult:
    """Paginate the search endpoint, run the cap and ordering probes, compute facts.

    May raise `RunPartial` (429 handling in `DiscoveryHttp`); the caller maps it.
    """
    pages: list[dict[str, Any]] = []
    all_items: list[Item] = []
    distinct: list[Item] = []
    seen: set[str] = set()
    duplicates = 0
    first_ids: list[str] = []
    start = 0
    status = "complete"
    note: str | None = None

    while True:
        if len(pages) >= max_pages:
            status = "incomplete"
            note = "max-pages ceiling reached"
            break
        params = {
            "start": str(start),
            "limit": str(page_size),
            "excludeChildren": "false",
            "sortBy": "startLocal",
            "sortOrder": "asc",
        }
        response, items = _fetch(http, recorder, token, f"search-page-{len(pages) + 1:04d}", params)
        if isinstance(response, RetriesExhausted):
            status, note = "incomplete", "search page retries exhausted"
            break
        if items is None:
            status = "incomplete"
            note = (
                f"search page returned HTTP {response.status}"
                if not 200 <= response.status < 300
                else "search page body not recognised"
            )
            break
        pages.append(
            {
                "item_count": len(items),
                "http_status": response.status,
                "headers": dict(response.headers.values),
            }
        )
        if not items:
            break
        if len(pages) == 1:
            first_ids = [_activity_id(i) for i in items]
        for item in items:
            all_items.append(item)
            activity_id = _activity_id(item)
            if activity_id in seen:
                duplicates += 1
            else:
                seen.add(activity_id)
                distinct.append(item)
        start += len(items)

    # Page-size cap probes.
    cap_probes: list[dict[str, Any]] = []
    for limit in CAP_PROBE_LIMITS:
        params = {"start": "0", "limit": str(limit)}
        response, items = _fetch(http, recorder, token, f"cap-probe-{limit}", params)
        probe: dict[str, Any] = {"limit": limit}
        if isinstance(response, RetriesExhausted):
            probe.update(error=response.error)
        else:
            probe["http_status"] = response.status
            probe["item_count"] = len(items) if items is not None else None
        cap_probes.append(probe)

    # Ordering stability probe: re-fetch the first page.
    ordering: dict[str, Any] | None = None
    if pages:
        params = {
            "start": "0",
            "limit": str(page_size),
            "excludeChildren": "false",
            "sortBy": "startLocal",
            "sortOrder": "asc",
        }
        response, items = _fetch(http, recorder, token, "ordering-probe", params)
        if items is not None:
            ordering = compare_first_page(first_ids, [_activity_id(i) for i in items])
        else:
            ordering = {"stable": None, "differing_positions": None}

    pagination: dict[str, Any] = {
        "inventory_status": status,
        "page_size_requested": page_size,
        "pages": pages,
        "duplicate_id_count": duplicates,
        "same_start_time_count": _same_start_time_count(distinct),
        "total_distinct": len(distinct),
    }
    return InventoryResult(
        items=distinct,
        inventory_status=status,
        partial=status != "complete",
        pagination=pagination,
        inventory=compute_facts(distinct),
        ordering_probe=ordering,
        cap_probes=cap_probes,
        note=note,
    )


__all__ = [
    "InventoryResult",
    "compare_first_page",
    "compute_facts",
    "parse_items",
    "run_inventory",
]
