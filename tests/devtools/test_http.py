from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from garmin_archive.devtools.discovery.http import (
    DiscoveryHttp,
    HttpResponse,
    RetriesExhausted,
    RunPartial,
    record_headers,
)
from garmin_archive.devtools.discovery.raw import RawRecorder


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    sleeps: list[float]


def make(
    handler: httpx.MockTransport, delay: float = 0.0
) -> tuple[DiscoveryHttp, Clock, list[float]]:
    clock = Clock()
    clock.sleeps = []
    http = DiscoveryHttp(delay=delay, sleep=clock.sleep, clock=clock, transport=handler)
    return http, clock, clock.sleeps


def test_persistent_500_four_requests_and_backoff_sleeps() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    http, _, sleeps = make(httpx.MockTransport(handler))
    result = http.get("/x")
    assert isinstance(result, RetriesExhausted)
    assert result.attempts == 4
    assert result.last_status == 500
    assert len(seen) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_5xx_then_success() -> None:
    codes = iter([503, 200])
    http, _, sleeps = make(httpx.MockTransport(lambda r: httpx.Response(next(codes), text="ok")))
    result = http.get("/x")
    assert isinstance(result, HttpResponse)
    assert result.attempts == 2
    assert result.body == b"ok"
    assert sleeps == [1.0]


def test_network_error_records_type_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom secret-host", request=request)

    http, _, sleeps = make(httpx.MockTransport(handler))
    result = http.get("/x")
    assert isinstance(result, RetriesExhausted)
    assert result.error == "ConnectError"
    assert result.last_status is None
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_after_300_is_partial() -> None:
    http, _, sleeps = make(
        httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "300"}))
    )
    with pytest.raises(RunPartial):
        http.get("/x")
    assert sleeps == []
    assert http.stats.count_429 == 1


def test_429_without_retry_after_is_partial() -> None:
    http, _, _ = make(httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(RunPartial):
        http.get("/x")


def test_429_once_then_success() -> None:
    codes = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(codes)
        return httpx.Response(code, headers={"Retry-After": "7"} if code == 429 else {})

    http, _, sleeps = make(httpx.MockTransport(handler))
    result = http.get("/x")
    assert isinstance(result, HttpResponse)
    assert result.status == 200
    assert sleeps == [7.0]
    assert http.stats.count_429 == 1
    assert http.stats.retry_after_seen == ["7"]


def test_second_429_is_partial() -> None:
    http, _, sleeps = make(
        httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "3"}))
    )
    with pytest.raises(RunPartial):
        http.get("/x")
    assert sleeps == [3.0]
    assert http.stats.count_429 == 2


def test_retry_after_http_date() -> None:
    codes = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(codes)
        headers = {"Retry-After": "Thu, 01 Jan 2026 00:00:10 GMT"} if code == 429 else {}
        return httpx.Response(code, headers=headers)

    http, _, sleeps = make(httpx.MockTransport(handler))
    assert isinstance(http.get("/x"), HttpResponse)
    assert sleeps == [10.0]


def test_cross_host_redirect_not_followed_and_auth_not_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    http, _, _ = make(httpx.MockTransport(handler))
    result = http.get("/file", headers={"Authorization": "Bearer tok"})
    assert isinstance(result, HttpResponse)
    assert result.status == 302
    assert result.redirect_host == "evil.example"
    assert http.stats.redirects == ["evil.example"]
    assert len(seen) == 1
    assert all(r.url.host != "evil.example" for r in seen)


def test_redirect_without_location_records_empty_host() -> None:
    http, _, _ = make(httpx.MockTransport(lambda r: httpx.Response(301)))
    result = http.get("/x")
    assert isinstance(result, HttpResponse)
    assert result.redirect_host == ""


def test_pacing_sleeps_remaining_delay() -> None:
    http, _, sleeps = make(httpx.MockTransport(lambda r: httpx.Response(200)), delay=1.0)
    http.get("/a")
    http.get("/b")
    assert sleeps == [1.0]


def test_header_recording_allowlist() -> None:
    headers = httpx.Headers(
        {
            "Content-Type": "application/json",
            "X-RateLimit-Remaining": "5",
            "Set-Cookie": "sid=secret",
            "Authorization": "Bearer secret",
            "X-Trace": "abc",
        }
    )
    rec = record_headers(headers)
    assert rec.values == {"content-type": "application/json", "x-ratelimit-remaining": "5"}
    assert rec.other_names == ["set-cookie", "x-trace"]
    assert "secret" not in repr(rec)
    assert "authorization" not in rec.other_names


def test_rate_limit_headers_noted_in_stats() -> None:
    http, _, _ = make(
        httpx.MockTransport(lambda r: httpx.Response(200, headers={"X-RateLimit-Limit": "100"}))
    )
    http.get("/x")
    assert http.stats.rate_limit_headers == {"x-ratelimit-limit": "100"}


def test_raw_recorder_writes_exact_bytes_and_filtered_headers(tmp_path: Path) -> None:
    base = tmp_path
    body = b'{"a":  1}\n\xff'
    http, clock, _ = make(
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json", "Set-Cookie": "s=1"},
            )
        )
    )
    result = http.get("/x")
    assert isinstance(result, HttpResponse)
    recorder = RawRecorder(base, clock=clock)
    path = recorder.record("My Label!", "get", "/x", {"a": "1"}, result)
    assert path.read_bytes() == body
    assert path.name == "0001-My_Label.json"
    line = recorder.requests_path.read_text(encoding="utf-8")
    assert "s=1" not in line
    assert "set-cookie" not in line
