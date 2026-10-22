from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from garmin_archive.devtools.discovery.credentials import SecretStr
from garmin_archive.devtools.discovery.http import DiscoveryHttp
from garmin_archive.devtools.discovery.inventory import (
    SEARCH_PATH,
    compare_first_page,
    compute_facts,
    parse_items,
    run_inventory,
)
from garmin_archive.devtools.discovery.raw import RawRecorder

TOKEN = "bearer-SECRET"


def act(
    i: int, start: str | None = None, type_key: str = "running", **extra: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "activityId": 9_000_000_000 + i,
        "activityType": {"typeKey": type_key, "parentTypeId": 17},
        "ownerId": 777,
    }
    item["startTimeLocal"] = start or f"2024-01-{i:02d} 08:00:00"
    item.update(extra)
    return item


class Server:
    def __init__(self, items: list[dict[str, Any]], *, cap: int | None = None) -> None:
        self.items = items
        self.requests: list[httpx.Request] = []
        self.first_page_override: list[dict[str, Any]] | None = None
        self.first_page_calls = 0
        self.fail_from_start: int | None = None
        self.cap = cap

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == SEARCH_PATH
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        start = int(request.url.params["start"])
        limit = int(request.url.params["limit"])
        if self.fail_from_start is not None and start >= self.fail_from_start and start > 0:
            return httpx.Response(500)
        page = self.items[start : start + min(limit, self.cap or limit)]
        if start == 0 and "sortBy" in request.url.params:
            self.first_page_calls += 1
            if self.first_page_calls == 2 and self.first_page_override is not None:
                page = self.first_page_override
        return httpx.Response(200, json=page, headers={"Content-Type": "application/json"})


def run(server: Server, tmp_path: Path, **kw: Any) -> Any:
    http = DiscoveryHttp(delay=0, sleep=lambda s: None, transport=httpx.MockTransport(server))
    return run_inventory(http, RawRecorder(tmp_path), SecretStr(TOKEN), **kw)


def test_pagination_complete_and_query_params(tmp_path: Path) -> None:
    server = Server([act(i) for i in range(1, 8)])
    result = run(server, tmp_path, page_size=3)
    assert result.inventory_status == "complete"
    assert result.partial is False
    assert len(result.items) == 7
    pages = result.pagination["pages"]
    assert [p["item_count"] for p in pages] == [3, 3, 1, 0]
    assert result.pagination["total_distinct"] == 7
    assert result.pagination["page_size_requested"] == 3
    # start advances by items returned; the final empty page ends it.
    page_reqs = [r for r in server.requests if "sortBy" in r.url.params]
    assert [r.url.params["start"] for r in page_reqs[:4]] == ["0", "3", "6", "7"]
    assert dict(page_reqs[0].url.params) == {
        "start": "0",
        "limit": "3",
        "excludeChildren": "false",
        "sortBy": "startLocal",
        "sortOrder": "asc",
    }
    assert pages[0]["headers"]["content-type"] == "application/json"


def test_empty_first_page_stops(tmp_path: Path) -> None:
    result = run(Server([]), tmp_path)
    assert result.inventory_status == "complete"
    assert result.items == []
    # The terminating empty page is recorded; nothing is ever requested past it.
    assert [p["item_count"] for p in result.pagination["pages"]] == [0]
    assert result.pagination["total_distinct"] == 0


def test_duplicate_id_across_pages(tmp_path: Path) -> None:
    items = [act(1), act(2), act(2), act(3)]
    result = run(Server(items), tmp_path, page_size=2)
    assert result.pagination["total_distinct"] == 3
    assert result.pagination["duplicate_id_count"] == 1
    assert len(result.items) == 3


def test_max_pages_incomplete_partial_still_returns_items(tmp_path: Path) -> None:
    result = run(Server([act(i) for i in range(1, 8)]), tmp_path, page_size=3, max_pages=1)
    assert result.inventory_status == "incomplete"
    assert result.partial is True
    assert len(result.items) == 3
    assert result.pagination["inventory_status"] == "incomplete"
    assert result.note
    assert result.ordering_probe is not None


def test_non_2xx_page_is_incomplete(tmp_path: Path) -> None:
    server = Server([act(i) for i in range(1, 8)])
    server.fail_from_start = 3
    result = run(server, tmp_path, page_size=3)
    assert result.inventory_status == "incomplete"
    assert len(result.items) == 3
    assert result.note is not None
    assert "retries exhausted" in result.note


def test_ordering_stable(tmp_path: Path) -> None:
    result = run(Server([act(i) for i in range(1, 5)]), tmp_path)
    assert result.ordering_probe == {"stable": True, "differing_positions": 0}


def test_ordering_instability_detected(tmp_path: Path) -> None:
    items = [act(i) for i in range(1, 5)]
    server = Server(items)
    server.first_page_override = [items[1], items[0], items[2], items[3]]
    result = run(server, tmp_path)
    assert result.ordering_probe == {"stable": False, "differing_positions": 2}


def test_compare_first_page_length_difference() -> None:
    assert compare_first_page(["a", "b"], ["a"]) == {"stable": False, "differing_positions": 1}


def test_cap_probes_record_item_counts(tmp_path: Path) -> None:
    server = Server([act(i) for i in range(1, 8)], cap=5)
    result = run(server, tmp_path, page_size=99)
    assert result.cap_probes == [
        {"limit": 200, "http_status": 200, "item_count": 5},
        {"limit": 1000, "http_status": 200, "item_count": 5},
    ]
    probe_reqs = [r for r in server.requests if "sortBy" not in r.url.params]
    assert [dict(r.url.params) for r in probe_reqs] == [
        {"start": "0", "limit": "200"},
        {"start": "0", "limit": "1000"},
    ]


def test_serialized_results_contain_no_raw_ids(tmp_path: Path) -> None:
    items = [act(i, parentId=9_000_000_099) for i in range(1, 4)]
    result = run(Server(items), tmp_path)
    dumped = json.dumps(
        [result.pagination, result.inventory, result.ordering_probe, result.cap_probes]
    )
    assert "9000000" not in dumped
    assert "ownerId" not in dumped
    assert "777" not in dumped


def test_raw_bodies_recorded(tmp_path: Path) -> None:
    server = Server([act(i) for i in range(1, 3)])
    http = DiscoveryHttp(delay=0, sleep=lambda s: None, transport=httpx.MockTransport(server))
    recorder = RawRecorder(tmp_path)
    run_inventory(http, recorder, SecretStr(TOKEN), page_size=2)
    names = sorted(p.name for p in recorder.raw_dir.iterdir())
    assert names[0] == "0001-search-page-0001.json"
    assert "search-page-0002" in names[1]
    assert any("cap-probe-200" in n for n in names)
    assert any("ordering-probe" in n for n in names)
    log = recorder.requests_path.read_text(encoding="utf-8")
    assert TOKEN not in log
    assert "Bearer" not in log


def test_facts_counts() -> None:
    items = [
        act(1, "2023-03-05 10:00:00"),
        act(2, "2024-11-01 10:00:00", type_key="cycling", isParent=True),
        act(3, "2024-11-01 10:00:00", parentId=9_000_000_002),
        act(4, manualActivity=True),
        act(5, ownerId=1),
    ]
    facts = compute_facts(items)
    assert facts["by_type_key"] == {"cycling": 1, "running": 4}
    assert facts["by_parent_type_id"] == {"17": 5}
    assert facts["child_count"] == 1
    assert facts["parent_count"] == 1
    assert facts["manual_count"] == 1
    assert facts["oldest_month"] == "2023-03"
    assert facts["newest_month"] == "2024-11"
    assert facts["owner_id_constant"] is False
    assert compute_facts(items[:4])["owner_id_constant"] is True


def test_same_start_time_count(tmp_path: Path) -> None:
    items = [act(1, "2024-01-01 08:00:00"), act(2, "2024-01-01 08:00:00"), act(3)]
    result = run(Server(items), tmp_path)
    assert result.pagination["same_start_time_count"] == 2


def test_parse_items_envelopes() -> None:
    assert parse_items(b"nope") is None
    assert parse_items(b'{"x": 1}') is None
    assert parse_items(b'{"activityList": [{"a": 1}, 3]}') == [{"a": 1}]
    assert parse_items(b'[{"a": 1}]') == [{"a": 1}]


class CapServer(Server):
    """Returns 500 whenever `limit` exceeds `max_limit`."""

    def __init__(self, items: list[dict[str, Any]], max_limit: int) -> None:
        super().__init__(items)
        self.max_limit = max_limit

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if int(request.url.params["limit"]) > self.max_limit:
            self.requests.append(request)
            return httpx.Response(500, text="boom")
        return super().__call__(request)


def test_adaptive_page_size_reduces_and_completes(tmp_path: Path) -> None:
    server = CapServer(
        [act(i, f"2024-01-01 08:{i // 60:02d}:{i % 60:02d}") for i in range(209)], 30
    )
    result = run(server, tmp_path)
    assert result.inventory_status == "complete"
    assert result.pagination["page_size_effective"] == 24
    assert result.pagination["page_size_reductions"] == [
        {"from": 99, "to": 49, "start": 0},
        {"from": 49, "to": 24, "start": 0},
    ]
    assert result.pagination["page_size_reduction_count"] == 2
    assert result.pagination["total_distinct"] == 209
    assert result.pagination["duplicate_id_count"] == 0
    # One attempt per intermediate size.
    assert [r.url.params["limit"] for r in server.requests[:3]] == ["99", "49", "24"]
    assert all(p.get("note") for p in result.cap_probes)


def test_adaptive_failure_at_every_size_is_bounded(tmp_path: Path) -> None:
    server = CapServer([act(i) for i in range(1, 8)], 0)
    result = run(server, tmp_path)
    assert result.inventory_status == "incomplete"
    pages = [r for r in server.requests if "sortBy" in r.url.params]
    # 99, 49, 24, 12 once each, then 10 with the full 4-attempt policy.
    assert [r.url.params["limit"] for r in pages] == ["99", "49", "24", "12"] + ["10"] * 4
    assert result.pagination["page_size_effective"] == 10


def test_failed_requests_are_logged_and_dir_exists(tmp_path: Path) -> None:
    server = CapServer([act(1)], 0)
    recorder = RawRecorder(tmp_path)
    assert recorder.raw_dir.is_dir()
    http = DiscoveryHttp(delay=0, sleep=lambda s: None, transport=httpx.MockTransport(server))
    run_inventory(http, recorder, SecretStr(TOKEN))
    entries = [json.loads(line) for line in recorder.requests_path.read_text().splitlines()]
    assert entries
    assert all(e["status"] == 500 and e["path"] == SEARCH_PATH for e in entries)
    assert not list(recorder.raw_dir.iterdir())
    assert TOKEN not in recorder.requests_path.read_text()


def test_transport_errors_are_logged(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-url", request=request)

    recorder = RawRecorder(tmp_path)
    http = DiscoveryHttp(delay=0, sleep=lambda s: None, transport=httpx.MockTransport(boom))
    run_inventory(http, recorder, SecretStr(TOKEN), page_size=10)
    entries = [json.loads(line) for line in recorder.requests_path.read_text().splitlines()]
    assert entries[0]["error"] == "ConnectError"
    assert entries[0]["status"] is None
    assert "secret-url" not in recorder.requests_path.read_text()
