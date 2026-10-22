"""HTTP client for the discovery tool: pacing, retries, redirects and header recording.

Never records or raises with header values other than the allowlisted ones, and
never records the Authorization header.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

BASE_URL = "https://personal.connectapi.garmin.com"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
MAX_RETRY_AFTER_SECONDS = 120.0
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

ALLOWLISTED_HEADERS = frozenset(
    {"content-type", "content-length", "etag", "last-modified", "retry-after", "date"}
)
NEVER_RECORD_HEADERS = frozenset({"authorization", "set-cookie", "proxy-authorization"})


class RunPartial(Exception):
    """Stop signal: the run must end with outcome `partial`. Message has no secrets."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_allowlisted(name: str) -> bool:
    lowered = name.lower()
    return lowered in ALLOWLISTED_HEADERS or "ratelimit" in lowered or "rate-limit" in lowered


@dataclass
class RecordedHeaders:
    """Allowlisted header values (lowercased names) plus the names of all others."""

    values: dict[str, str] = field(default_factory=lambda: {})
    other_names: list[str] = field(default_factory=lambda: [])


def record_headers(headers: httpx.Headers) -> RecordedHeaders:
    recorded = RecordedHeaders()
    for name in headers:
        lowered = name.lower()
        if lowered in NEVER_RECORD_HEADERS:
            # Set-Cookie: name only; Authorization: not even the name.
            if lowered == "set-cookie" and lowered not in recorded.other_names:
                recorded.other_names.append(lowered)
            continue
        if is_allowlisted(lowered):
            recorded.values[lowered] = headers[name]
        elif lowered not in recorded.other_names:
            recorded.other_names.append(lowered)
    recorded.other_names.sort()
    return recorded


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: RecordedHeaders
    elapsed_ms: int
    attempts: int
    redirect_host: str | None = None


@dataclass
class RetriesExhausted:
    """All attempts failed (network error or 5xx). `error` is an exception type or status."""

    attempts: int
    error: str
    last_status: int | None = None


@dataclass
class HttpStats:
    count_429: int = 0
    retry_after_seen: list[str] = field(default_factory=lambda: [])
    rate_limit_headers: dict[str, str] = field(default_factory=lambda: {})
    redirects: list[str] = field(default_factory=lambda: [])
    other_header_names: set[str] = field(default_factory=lambda: set())


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    return max(seconds, 0.0)


def _host_of(location: str | None) -> str | None:
    if not location:
        return None
    return urlsplit(location).hostname


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DiscoveryHttp:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        delay: float = 1.0,
        sleep: Callable[[float], None],
        clock: Callable[[], datetime] = _utcnow,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            follow_redirects=False,
            timeout=TIMEOUT,
            transport=transport,
        )
        self._delay = delay
        self._sleep = sleep
        self._clock = clock
        self._last_end: datetime | None = None
        self.stats = HttpStats()

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        if self._last_end is None:
            return
        wait = self._delay - (self._clock() - self._last_end).total_seconds()
        if wait > 0:
            self._sleep(wait)

    def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
    ) -> httpx.Response:
        return self._client.request(
            method,
            url,
            params=params,
            headers=headers,
            data=data,
            follow_redirects=False,
        )

    def _note_headers(self, recorded: RecordedHeaders) -> None:
        for name, value in recorded.values.items():
            if name == "retry-after":
                self.stats.retry_after_seen.append(value)
            elif "ratelimit" in name or "rate-limit" in name:
                self.stats.rate_limit_headers[name] = value
        self.stats.other_header_names.update(recorded.other_names)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> HttpResponse | RetriesExhausted:
        """Perform one logical request (with retries). Raises RunPartial on 429 stop."""
        self._pace()
        attempts = 0
        retries_5xx = 0
        retried_429 = False
        last_error = ""
        last_status: int | None = None
        try:
            while True:
                attempts += 1
                started = self._clock()
                response: httpx.Response | None = None
                try:
                    response = self._send(method, url, params, headers, data)
                except httpx.HTTPError as exc:
                    # Type name only: messages could embed URLs or header values.
                    last_error = type(exc).__name__
                    last_status = None
                ended = self._clock()
                if response is not None:
                    status = response.status_code
                    recorded = record_headers(response.headers)
                    self._note_headers(recorded)
                    if status == 429:
                        self.stats.count_429 += 1
                        wait = _parse_retry_after(response.headers.get("retry-after"), ended)
                        if retried_429:
                            raise RunPartial("second 429 for the same request")
                        if wait is None or wait > MAX_RETRY_AFTER_SECONDS:
                            raise RunPartial("429 with missing or excessive Retry-After")
                        retried_429 = True
                        self._sleep(wait)
                        continue
                    if status < 500:
                        redirect_host = None
                        if 300 <= status < 400:
                            location = response.headers.get("location")
                            host = _host_of(location)
                            redirect_host = host or ""
                            self.stats.redirects.append(redirect_host)
                        return HttpResponse(
                            status=status,
                            body=response.content,
                            headers=recorded,
                            elapsed_ms=int((ended - started).total_seconds() * 1000),
                            attempts=attempts,
                            redirect_host=redirect_host,
                        )
                    last_error = f"HTTP {status}"
                    last_status = status
                if retries_5xx >= len(BACKOFF_SECONDS):
                    return RetriesExhausted(attempts, last_error, last_status)
                self._sleep(BACKOFF_SECONDS[retries_5xx])
                retries_5xx += 1
        finally:
            self._last_end = self._clock()

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse | RetriesExhausted:
        return self.request("GET", path, params=params, headers=headers)
