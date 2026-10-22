"""Network-side activity discovery: the paging loop with page-size fallback."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from garmin_archive.api import SEARCH_PATH, ApiResponse, search_params
from garmin_archive.archive import (
    KIND_ORIGINAL_FILE,
    KIND_SEARCH_PAGE,
    KIND_SUMMARY,
    KIND_WORKOUTS,
    STATE_FAILED,
    ArchiveReader,
    ArchiveWriter,
    canonical_json_hash,
    search_page_key,
)
from garmin_archive.models import (
    AccountMismatchError,
    ApiTransportError,
    Clock,
    ContractError,
    GarminArchiveError,
    RateLimitError,
    TransportError,
)
from garmin_archive.storage.connection import transaction
from garmin_archive.storage.metadata import get_account_binding, get_metadata, set_metadata
from garmin_archive.sync.runs import SyncCounters

PAGE_SIZE_KEY = "search_page_size"
DEFAULT_PAGE_SIZE = 49
MIN_PAGE_SIZE = 10
DEFAULT_PAGE_CEILING = 10_000

RECENT_WINDOW = timedelta(days=14)
_FETCH_KINDS: tuple[str, ...] = (KIND_SUMMARY, KIND_WORKOUTS, KIND_ORIGINAL_FILE)

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


def _parse_utc_start(value: object) -> datetime | None:
    """Parse an offset-free ``startTimeGMT`` as UTC; ``None`` when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text_or_none(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    return str(value)


def _item_type_key(item: Mapping[str, object]) -> str | None:
    activity_type = item.get("activityType")
    if isinstance(activity_type, dict):
        return _text_or_none(cast("dict[str, object]", activity_type).get("typeKey"))
    return None


@dataclass
class PageRecorder:
    """Page callback: records one search page atomically and queues resources to fetch."""

    conn: sqlite3.Connection
    writer: ArchiveWriter
    reader: ArchiveReader
    clock: Clock
    counters: SyncCounters
    sync_run_id: int | None = None
    full: bool = False
    seen_ids: set[str] = field(default_factory=lambda: set[str]())

    def __call__(
        self,
        response: ApiResponse,
        params: Mapping[str, str],
        items: list[dict[str, object]],
    ) -> None:
        self._check_account(items)
        discovered = new = changed = 0
        newly_seen: set[str] = set()
        threshold = self.clock.now().astimezone(UTC) - RECENT_WINDOW
        with self.writer.batch():
            result = self.writer.record_response(
                resource_key=search_page_key(params),
                kind=KIND_SEARCH_PAGE,
                activity_id=None,
                sync_run_id=self.sync_run_id,
                method="GET",
                path=SEARCH_PATH,
                request_params=params,
                http_status=response.status,
                content_type=response.content_type,
                body=response.body,
            )
            for index, item in enumerate(items):
                activity_id = str(item["activityId"])
                first_on_run = activity_id not in self.seen_ids and activity_id not in newly_seen
                prior_hash = self.reader.prior_search_item_hash(activity_id)
                inserted = self.writer.upsert_activity(
                    activity_id=activity_id,
                    owner_profile_id=_text_or_none(item.get("ownerId")),
                    parent_activity_id=_text_or_none(item.get("parentId")),
                    type_key=_item_type_key(item),
                )
                is_changed = prior_hash is not None and prior_hash != canonical_json_hash(item)
                self.writer.record_search_item(
                    activity_id=activity_id,
                    search_page_payload_id=result.payload_id,
                    json_pointer=f"/{index}",
                )
                start = _parse_utc_start(item.get("startTimeGMT"))
                recent = start is not None and start >= threshold
                if self.full or inserted or is_changed:
                    kinds = _FETCH_KINDS
                else:
                    # Garmin recalculates summary metrics after upload; workouts and original
                    # files don't change, so the recent window only refreshes the summary.
                    states = self.reader.resource_states(activity_id)
                    kinds = tuple(
                        k
                        for k in _FETCH_KINDS
                        if states.get(k, STATE_FAILED) == STATE_FAILED
                        or (recent and k == KIND_SUMMARY)
                    )
                if kinds:
                    self.writer.mark_pending(activity_id=activity_id, kinds=kinds)
                if first_on_run:
                    newly_seen.add(activity_id)
                    discovered += 1
                    new += inserted
                    changed += is_changed
        # Only reached once the page transaction committed.
        self.seen_ids |= newly_seen
        self.counters.discovered += discovered
        self.counters.new += new
        self.counters.changed += changed

    def _check_account(self, items: list[dict[str, object]]) -> None:
        bound = get_account_binding(self.conn)
        if bound is None:
            return
        for item in items:
            owner = _text_or_none(item.get("ownerId"))
            if owner is not None and owner != bound:
                raise AccountMismatchError("Search page belongs to a different Garmin account")
