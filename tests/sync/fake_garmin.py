"""Configurable fake Garmin Personal API on ``httpx.MockTransport`` for sync tests.

Bodies are built from copies of the committed sanitized contract fixtures with
synthetic IDs, profile and start times. Original files are in-memory ZIPs. Nothing
touches the network or the filesystem beyond reading the fixtures.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from garmin_archive.api import SEARCH_PATH, TOKEN_PATH
from helpers.fitbuilder import build_fit
from helpers.zipbuilder import build_zip

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "contract"
BASE_FIXTURES = ("running-1", "pool_swim-1", "open_water_swim-1", "cardio-1", "other-1")
UNKNOWN_TYPE_KEY = "synthetic_unknown_sport"
MULTISPORT_TYPE_KEY = "multi_sport"
ID_BASE = 9_100_000_000
FAKE_PROFILE_ID = "900000001"
FAKE_API_KEY = "fake-api-key-for-leak-checks"
FAKE_TOKEN = "fake-bearer-token-for-leak-checks"
LOCAL_OFFSET = timedelta(hours=2)

KINDS = ("search", "summary", "workouts", "file")


def _load(kind: str, name: str) -> Any:
    return json.loads((CONTRACT_DIR / kind / f"{name}.json").read_text(encoding="utf-8"))


def _gmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, indent=2).encode("utf-8")


@dataclass
class _Failure:
    kind: str
    activity_id: str | None
    status: int
    remaining: int | None


class FakeGarmin:
    """A fake Personal API. Use ``client()`` / ``transport`` to talk to it."""

    def __init__(
        self,
        count: int = 250,
        *,
        profile_id: str = FAKE_PROFILE_ID,
        api_key: str = FAKE_API_KEY,
        token: str = FAKE_TOKEN,
        first_start: datetime = datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        spacing: timedelta = timedelta(hours=12),
        unknown_index: int | None = 20,
        multisport_parent_index: int | None = 10,
        children_per_parent: int = 2,
    ) -> None:
        self.profile_id = profile_id
        self.api_key = api_key
        self.token = token
        # Activity IDs in search order (ascending start time).
        self.activity_ids: list[str] = []
        self.parent_id: str | None = None
        self.child_ids: list[str] = []
        self.unknown_id: str | None = None
        self.search_items: list[dict[str, Any]] = []
        self.summaries: dict[str, bytes] = {}
        self.workouts: dict[str, bytes] = {}
        self.files: dict[str, bytes] = {}
        self.start_times: dict[str, datetime] = {}

        # Configurable behaviour.
        self.fail_above_limit: int | None = None
        self.page_cap: int | None = None
        self.reject_auth = False
        self.rate_limit_from: int | None = None
        self.rate_limit_count: int | None = None
        self.on_request: Callable[[str, str | None, httpx.Request], None] | None = None
        self._failures: list[_Failure] = []
        self._connections: list[sqlite3.Connection] = []

        # Recording.
        self.counts: Counter[str] = Counter()
        self.per_activity: Counter[tuple[str, str]] = Counter()
        self.requests: list[tuple[str, str | None, dict[str, str]]] = []
        self.search_limits: list[int] = []
        self.transaction_violations: list[tuple[str, str | None]] = []
        self.data_request_count = 0

        self._build(
            count,
            first_start,
            spacing,
            unknown_index,
            multisport_parent_index,
            children_per_parent,
        )
        self.transport = httpx.MockTransport(self._handle)

    # ---- construction -------------------------------------------------

    def _build(
        self,
        count: int,
        first_start: datetime,
        spacing: timedelta,
        unknown_index: int | None,
        parent_index: int | None,
        children: int,
    ) -> None:
        bases = {
            n: (_load("search-items", n), _load("summaries", n), _load("workouts", n))
            for n in BASE_FIXTURES
        }
        child_idx = (
            set(range(parent_index + 1, parent_index + 1 + children))
            if parent_index is not None
            else set()
        )
        ids = [str(ID_BASE + i) for i in range(count)]
        if parent_index is not None and parent_index < count:
            self.parent_id = ids[parent_index]
            self.child_ids = [ids[i] for i in sorted(child_idx) if i < count]
        for i, aid in enumerate(ids):
            name = BASE_FIXTURES[i % len(BASE_FIXTURES)]
            item, summary, workouts = (json.loads(json.dumps(x)) for x in bases[name])
            start = first_start + i * spacing
            self._synthesize(item, summary, aid, start)
            if i == unknown_index:
                self.unknown_id = aid
                self._set_type(item, summary, 9999, UNKNOWN_TYPE_KEY)
            if aid == self.parent_id:
                self._set_type(item, summary, 89, MULTISPORT_TYPE_KEY)
                item["isParent"] = True
                summary["isMultiSportParent"] = True
                summary["metadataDTO"]["childIds"] = [int(c) for c in self.child_ids]
            elif i in child_idx and self.parent_id is not None:
                item["parentId"] = int(self.parent_id)
                summary["parentId"] = int(self.parent_id)
            self.activity_ids.append(aid)
            self.start_times[aid] = start
            self.search_items.append(item)
            self.summaries[aid] = _dump(summary)
            self.workouts[aid] = _dump(workouts)
            self.files[aid] = build_zip({f"{aid}_ACTIVITY.fit": build_fit(1 + i % 7)})

    def _synthesize(
        self, item: dict[str, Any], summary: dict[str, Any], aid: str, start: datetime
    ) -> None:
        profile = int(self.profile_id)
        local = start + LOCAL_OFFSET
        item.update(
            activityId=int(aid),
            ownerId=profile,
            startTimeGMT=_gmt(start),
            startTimeLocal=_gmt(local),
        )
        summary.update(activityId=int(aid), userProfileId=profile)
        summary["activityUUID"] = {"uuid": f"00000000-0000-4000-8000-{int(aid):012d}"}
        summary["metadataDTO"]["userInfoDto"]["userProfilePk"] = profile
        dto = summary.get("summaryDTO")
        if isinstance(dto, dict):
            if "startTimeGMT" in dto:
                dto["startTimeGMT"] = start.strftime("%Y-%m-%dT%H:%M:%S.0")
            if "startTimeLocal" in dto:
                dto["startTimeLocal"] = local.strftime("%Y-%m-%dT%H:%M:%S.0")

    @staticmethod
    def _set_type(item: dict[str, Any], summary: dict[str, Any], type_id: int, key: str) -> None:
        for target in (item["activityType"], summary["activityTypeDTO"]):
            target.update(typeId=type_id, typeKey=key)

    # ---- public configuration ----------------------------------------

    @property
    def secrets(self) -> tuple[str, str]:
        """The fake API key and token, for leak checks on stdout/stderr/logs/DB."""
        return (self.api_key, self.token)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, follow_redirects=False)

    def watch_connection(self, conn: sqlite3.Connection) -> None:
        """Assert on every request that ``conn`` has no open transaction."""
        self._connections.append(conn)

    def fail(
        self, kind: str, activity_id: str | None = None, status: int = 503, times: int | None = None
    ) -> None:
        """Answer ``status`` for a resource: ``times`` requests, or until cleared when None.

        ``kind`` is one of search, summary, workouts, file. ``activity_id`` None matches all
        activities (for ``search`` it is always None).
        """
        assert kind in KINDS
        self._failures.append(_Failure(kind, activity_id, status, times))

    def clear_failures(self) -> None:
        self._failures.clear()

    def rate_limit(self, at_request: int, times: int | None = None) -> None:
        """429 on data requests number ``at_request`` (1-based, incl. retries) onward."""
        self.rate_limit_from = at_request
        self.rate_limit_count = times

    def clear_rate_limit(self) -> None:
        self.rate_limit_from = None
        self.rate_limit_count = None

    def mutate_search_item(self, activity_id: str, **changes: Any) -> None:
        self.search_items[self.activity_ids.index(activity_id)].update(changes)

    def mismatch_profile(
        self, activity_ids: Iterable[str] | None = None, profile_id: str = "900000099"
    ) -> None:
        """Make summaries report another ``userProfileId`` (all activities when None)."""
        for aid in self.activity_ids if activity_ids is None else activity_ids:
            body = json.loads(self.summaries[aid])
            body["userProfileId"] = int(profile_id)
            self.summaries[aid] = _dump(body)

    def mismatch_owner(
        self, activity_ids: Iterable[str] | None = None, owner_id: str = "900000099"
    ) -> None:
        """Make search items report another ``ownerId`` (all activities when None)."""
        for aid in self.activity_ids if activity_ids is None else activity_ids:
            self.mutate_search_item(aid, ownerId=int(owner_id))

    # ---- counting -----------------------------------------------------

    def count(self, kind: str) -> int:
        return self.counts[kind]

    def count_for(self, kind: str, activity_id: str) -> int:
        return self.per_activity[(kind, activity_id)]

    def reset_counts(self) -> None:
        self.counts.clear()
        self.per_activity.clear()
        self.requests.clear()
        self.search_limits.clear()
        self.data_request_count = 0

    # ---- request handling ---------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        kind, aid = self._classify(path)
        self.counts[kind] += 1
        self.requests.append((kind, aid, dict(request.url.params)))
        if aid is not None:
            self.per_activity[(kind, aid)] += 1
        for conn in self._connections:
            if conn.in_transaction:
                self.transaction_violations.append((kind, aid))
                raise AssertionError(f"SQLite transaction open during HTTP request ({kind}, {aid})")
        if self.on_request is not None:
            self.on_request(kind, aid, request)
        if kind == "token":
            return self._token(request)
        if kind == "unknown":
            return httpx.Response(404)
        if self.reject_auth or request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        self.data_request_count += 1
        if self._rate_limited():
            return httpx.Response(429)
        status = self._injected_status(kind, aid)
        if status is not None:
            return httpx.Response(status)
        if kind == "search":
            return self._search(request)
        assert aid is not None
        if aid not in self.summaries:
            return httpx.Response(404)
        if kind == "summary":
            return self._json(self.summaries[aid])
        if kind == "workouts":
            return self._json(self.workouts[aid])
        return httpx.Response(
            200, content=self.files[aid], headers={"content-type": "application/zip"}
        )

    @staticmethod
    def _classify(path: str) -> tuple[str, str | None]:
        if path == TOKEN_PATH:
            return "token", None
        if path == SEARCH_PATH:
            return "search", None
        if path.startswith("/download-service/files/activity/"):
            return "file", path.rsplit("/", 1)[1]
        if path.startswith("/activity-service/activity/"):
            rest = path.removeprefix("/activity-service/activity/").split("/")
            if len(rest) == 1:
                return "summary", rest[0]
            if len(rest) == 2 and rest[1] == "workouts":
                return "workouts", rest[0]
        return "unknown", None

    @staticmethod
    def _json(body: bytes) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json;charset=UTF-8"}
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        if (
            request.method != "POST"
            or request.headers.get("Authorization") != f"ApiKey {self.api_key}"
        ):
            return httpx.Response(401)
        return httpx.Response(200, text=self.token + "\n", headers={"content-type": "text/plain"})

    def _rate_limited(self) -> bool:
        if self.rate_limit_from is None or self.data_request_count < self.rate_limit_from:
            return False
        if self.rate_limit_count is None:
            return True
        return self.data_request_count < self.rate_limit_from + self.rate_limit_count

    def _injected_status(self, kind: str, aid: str | None) -> int | None:
        for failure in self._failures:
            if failure.kind != kind or failure.activity_id not in (None, aid):
                continue
            if failure.remaining is not None:
                if failure.remaining <= 0:
                    continue
                failure.remaining -= 1
            return failure.status
        return None

    def _search(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = int(params.get("start", "0"))
        limit = int(params.get("limit", "20"))
        self.search_limits.append(limit)
        if self.fail_above_limit is not None and limit > self.fail_above_limit:
            return httpx.Response(500)
        effective = limit if self.page_cap is None else min(limit, self.page_cap)
        page = self.search_items[start : start + effective]
        return self._json(_dump(page) if page else b"[]")
