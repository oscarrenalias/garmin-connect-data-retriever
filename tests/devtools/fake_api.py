"""A fake Garmin Personal API served through `httpx.MockTransport` (spec AC5).

Everything is synthetic. The fake records every request so tests can assert on what was sent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from garmin_archive.devtools.discovery.samples import FILE_PATH, SUMMARY_PATH, WORKOUTS_PATH
from garmin_archive.devtools.discovery.token import SEARCH_PATH, TOKEN_PATH
from helpers.fitbuilder import build_fit
from helpers.zipbuilder import build_zip

API_KEY = "fake-api-key-SECRET-Qx7"
TOKEN = "fake.token.SECRET-Zk9"
PROFILE_ID = 7700112233
PAGE_SIZE = 3

RUNNING_ID = 5100000001
MULTI_ID = 5100000002
CHILD_1_ID = 5100000003
CHILD_2_ID = 5100000004
MANUAL_ID = 5100000005
POOL_ID = 5100000006
OTHER_ID = 5100000007
ACTIVITY_IDS = (RUNNING_ID, MULTI_ID, CHILD_1_ID, CHILD_2_ID, MANUAL_ID, POOL_ID, OTHER_ID)


def _item(activity_id: int, day: int, type_key: str, **extra: Any) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": f"Secret Name {activity_id}",
        "startTimeLocal": f"2024-03-{day:02d} 07:00:00",
        "ownerId": PROFILE_ID,
        "activityType": {"typeId": 1, "typeKey": type_key, "parentTypeId": 17},
        **extra,
    }


ITEMS: dict[int, dict[str, Any]] = {
    RUNNING_ID: _item(RUNNING_ID, 1, "running"),
    MULTI_ID: _item(MULTI_ID, 2, "multi_sport", isParent=True, childIds=[CHILD_1_ID, CHILD_2_ID]),
    CHILD_1_ID: _item(CHILD_1_ID, 3, "cycling", parentId=MULTI_ID),
    CHILD_2_ID: _item(CHILD_2_ID, 4, "running", parentId=MULTI_ID),
    MANUAL_ID: _item(MANUAL_ID, 5, "yoga", manualActivity=True),
    POOL_ID: _item(POOL_ID, 6, "lap_swimming"),
    OTHER_ID: _item(OTHER_ID, 7, "hiking"),
}
# Search order: the running activity appears again on page 2 (the one duplicate ID).
SEARCH_ORDER = (
    RUNNING_ID,
    MULTI_ID,
    CHILD_1_ID,
    RUNNING_ID,
    CHILD_2_ID,
    MANUAL_ID,
    POOL_ID,
    OTHER_ID,
)


def _json(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )


class FakeGarminApi:
    """Callable `httpx.MockTransport` handler. Redirects, 5xx and 429s are opt-in knobs."""

    def __init__(self, *, reorder_first_page: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.reorder_first_page = reorder_first_page
        self._first_page_served = 0
        self._sent_429 = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == TOKEN_PATH:
            assert request.method == "POST"
            assert request.headers["authorization"] == f"ApiKey {API_KEY}"
            return _json({"accessToken": TOKEN, "expiresIn": 3600})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if path == SEARCH_PATH:
            return self._search(request)
        return self._activity(request, path)

    def _search(self, request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        limit = int(request.url.params["limit"])
        if limit == 1:
            return _json([ITEMS[RUNNING_ID]])
        if limit in (200, 1000):  # the cap probes: the server caps at 100
            return _json([_item(9_000_000 + n, 1 + n % 28, "running") for n in range(100)])
        order = list(SEARCH_ORDER)
        if start == 0 and self.reorder_first_page:
            self._first_page_served += 1
            if self._first_page_served > 1:
                order[0], order[1] = order[1], order[0]
        return _json([ITEMS[i] for i in order[start : start + limit]])

    def _activity(self, request: httpx.Request, path: str) -> httpx.Response:
        for template, handler in (
            (WORKOUTS_PATH, self._workouts),
            (FILE_PATH, self._file),
            (SUMMARY_PATH, self._summary),
        ):
            prefix, _, suffix = template.partition("{id}")
            if path.startswith(prefix) and path.endswith(suffix):
                activity_id = int(path[len(prefix) : len(path) - len(suffix)])
                return handler(activity_id)
        return httpx.Response(404)

    def _summary(self, activity_id: int) -> httpx.Response:
        body = {
            **ITEMS[activity_id],
            "userProfileId": PROFILE_ID,
            "description": f"Secret description {activity_id}",
            "summaryDTO": {"distance": 1234.5, "startLatitude": 51.5007, "startLongitude": -0.1246},
        }
        return _json(body)

    def _workouts(self, activity_id: int) -> httpx.Response:
        if activity_id == RUNNING_ID and not self._sent_429:
            self._sent_429 = True
            return httpx.Response(429, headers={"Retry-After": "2", "X-RateLimit-Limit": "60"})
        if activity_id == POOL_ID:
            return _json([{"workoutId": 1, "name": "intervals"}, {"workoutId": 2}])
        return _json([])

    def _file(self, activity_id: int) -> httpx.Response:
        if activity_id == MANUAL_ID:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/plain"})
        fit = build_fit(record_count=2)
        if activity_id == RUNNING_ID:
            body = build_zip({f"{activity_id}_ACTIVITY.fit": fit})
            return httpx.Response(200, content=body, headers={"content-type": "application/zip"})
        return httpx.Response(
            200, content=fit, headers={"content-type": "application/octet-stream"}
        )
