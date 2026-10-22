"""Network-side activity discovery: the paging loop with page-size fallback."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from garmin_archive.api import ApiResponse, search_params
from garmin_archive.models import (
    ApiTransportError,
    ContractError,
    GarminArchiveError,
    RateLimitError,
    TransportError,
)
from garmin_archive.storage.connection import transaction
from garmin_archive.storage.metadata import get_metadata, set_metadata

PAGE_SIZE_KEY = "search_page_size"
DEFAULT_PAGE_SIZE = 49
MIN_PAGE_SIZE = 10
DEFAULT_PAGE_CEILING = 10_000

REASON_REPEATED_PAGE = "repeated_page"
REASON_PAGE_CEILING = "page_ceiling"
REASON_PAGE_FAILED = "page_failed"
REASON_INTERRUPTED = "interrupted"

PageCallback = Callable[[ApiResponse, Mapping[str, str], "list[dict[str, object]]"], None]


class SearchClient(Protocol):
    """The slice of ``GarminApiClient`` that discovery needs."""

    def search_activities(
        self, start: int, limit: int, *, max_attempts: int | None = None
    ) -> ApiResponse: ...


@dataclass(frozen=True)
class DiscoveryOutcome:
    """Result of a discovery run; ``incomplete_reason`` is set unless the end was reached."""

    reached_end: bool
    incomplete_reason: str | None = None
    last_error: ApiTransportError | None = None
    pages: int = 0
    page_size: int = DEFAULT_PAGE_SIZE


def load_page_size(conn: sqlite3.Connection) -> int:
    """Return the stored working page size, or the default when absent or invalid."""
    value = get_metadata(conn, PAGE_SIZE_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value >= MIN_PAGE_SIZE:
        return value
    return DEFAULT_PAGE_SIZE


def _store_page_size(conn: sqlite3.Connection, size: int) -> None:
    with transaction(conn):
        set_metadata(conn, PAGE_SIZE_KEY, size)


def parse_search_page(response: ApiResponse) -> list[dict[str, object]]:
    """Parse a search body as a JSON array of objects, each with an ``activityId``."""
    try:
        parsed: object = json.loads(response.body)
    except ValueError:
        raise ContractError("Search page is not valid JSON") from None
    if not isinstance(parsed, list):
        raise ContractError("Search page is not a JSON array", details={"status": response.status})
    items: list[dict[str, object]] = []
    for entry in cast("list[object]", parsed):
        if not isinstance(entry, dict):
            raise ContractError("Search page item is not an object")
        item = cast("dict[str, object]", entry)
        if item.get("activityId") is None:
            raise ContractError("Search page item has no activityId")
        items.append(item)
    return items


def page_activity_ids(items: list[dict[str, object]]) -> frozenset[str]:
    """Activity IDs of a parsed page, as TEXT."""
    return frozenset(str(item["activityId"]) for item in items)


def discover_activities(
    client: SearchClient,
    conn: sqlite3.Connection,
    on_page: PageCallback,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    page_ceiling: int = DEFAULT_PAGE_CEILING,
) -> DiscoveryOutcome:
    """Page through activity search until an empty page.

    Each non-empty page is handed to ``on_page`` (which owns its own transaction).
    No transaction is held across an HTTP call. Auth failures and exceptions from
    ``on_page`` propagate; other API errors end discovery as incomplete.
    """
    size = load_page_size(conn)
    start = 0
    pages = 0
    previous_ids: frozenset[str] | None = None
    shrinking = False

    def outcome(
        reached_end: bool, reason: str | None = None, error: ApiTransportError | None = None
    ) -> DiscoveryOutcome:
        return DiscoveryOutcome(reached_end, reason, error, pages, size)

    while True:
        if should_stop():
            return outcome(False, REASON_INTERRUPTED)
        if pages >= page_ceiling:
            return outcome(False, REASON_PAGE_CEILING)
        try:
            response = client.search_activities(start, size, max_attempts=1 if shrinking else None)
            items = parse_search_page(response)
        except TransportError as exc:
            status = exc.details.get("status")
            if isinstance(status, int) and status >= 500 and size > MIN_PAGE_SIZE:
                size = max(MIN_PAGE_SIZE, size // 2)
                shrinking = True
                _store_page_size(conn, size)
                continue
            return outcome(False, REASON_PAGE_FAILED, exc)
        except (RateLimitError, ContractError) as exc:
            return outcome(False, REASON_PAGE_FAILED, exc)
        except GarminArchiveError:
            raise
        if not items:
            if size != load_page_size(conn):
                _store_page_size(conn, size)
            return outcome(True)
        ids = page_activity_ids(items)
        if ids == previous_ids:
            return outcome(False, REASON_REPEATED_PAGE)
        on_page(response, search_params(start, size), items)
        pages += 1
        previous_ids = ids
        shrinking = False
        start += len(items)
