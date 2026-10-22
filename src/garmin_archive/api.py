"""Garmin Personal API client core: token lifecycle and status mapping (SPEC §2, §4)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from garmin_archive.models import (
    AuthenticationFailedError,
    AuthorizationError,
    ContractError,
    RateLimitError,
    TransportError,
)

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SEARCH_PATH = "/activitylist-service/activities/search/activities"
DEFAULT_MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_SECONDS = 300
BACKOFF_BASE_SECONDS = 1.0
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


@dataclass(frozen=True)
class ApiResponse:
    """A raw HTTP response; the body is never parsed."""

    status: int
    content_type: str | None
    body: bytes
    retry_after: str | None = None


def default_http_client() -> httpx.Client:
    """Build the default client: no redirect following and finite timeouts."""
    return httpx.Client(follow_redirects=False, timeout=DEFAULT_TIMEOUT)


class GarminApiClient:
    """Thin Personal API client holding one in-memory token per instance."""

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
    ) -> None:
        """Create a client; ``http_client`` and ``sleep`` are injectable for tests.

        The key and the token it yields are held in memory only.
        """
        self._api_key = api_key
        self._http = http_client if http_client is not None else default_http_client()
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None

    def _fetch_token(self) -> str:
        response = self._send(
            "POST", TOKEN_PATH, None, {"Authorization": f"ApiKey {self._api_key}"}
        )
        status = response.status
        if status in (401, 403):
            raise AuthenticationFailedError(
                f"Token endpoint rejected the API key (HTTP {status})",
                details={"status": status},
            )
        self._raise_for_status(response, "token")
        token = response.body.decode("utf-8", errors="replace").strip()
        if status != 200 or not token:
            raise ContractError(
                "Token endpoint returned an unusable response",
                details={"status": status},
            )
        self._token = token
        return token

    def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
    ) -> ApiResponse:
        """Single-attempt send; transport failures map to ``TransportError``."""
        try:
            response = self._http.request(
                method, f"{self._base_url}{path}", params=params, headers=dict(headers)
            )
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Network failure calling Garmin API: {type(exc).__name__}"
            ) from None
        return ApiResponse(
            status=response.status_code,
            content_type=response.headers.get("content-type"),
            body=response.content,
            retry_after=response.headers.get("retry-after"),
        )

    @staticmethod
    def _raise_for_status(response: ApiResponse, resource: str) -> None:
        status = response.status
        details: dict[str, object] = {"status": status, "resource": resource}
        if 200 <= status < 300 or status == 404:
            return
        if status == 403:
            raise AuthorizationError(
                f"Not authorized to access {resource} (HTTP 403)", details=details
            )
        if status == 429:
            raise RateLimitError(f"Rate limited fetching {resource}", details=details)
        if status >= 500:
            raise TransportError(
                f"Upstream error fetching {resource} (HTTP {status})", details=details
            )
        raise ContractError(f"Unexpected HTTP {status} fetching {resource}", details=details)

    def _attempt(self, method: str, path: str, params: Mapping[str, str] | None) -> ApiResponse:
        """One authenticated attempt, renewing the token once on a 401."""
        token = self._token if self._token is not None else self._fetch_token()
        response = self._send(method, path, params, {"Authorization": f"Bearer {token}"})
        if response.status != 401:
            return response
        token = self._fetch_token()
        response = self._send(method, path, params, {"Authorization": f"Bearer {token}"})
        if response.status == 401:
            raise AuthenticationFailedError(
                "Garmin API rejected a freshly issued token (HTTP 401)",
                details={"status": 401, "resource": path},
            )
        return response

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        """Parse Retry-After as integer seconds; anything else is ignored."""
        if value is None:
            return None
        value = value.strip()
        if not value.isascii() or not value.isdigit():
            return None
        return int(value)

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> ApiResponse:
        """Perform a data request with retry on transient failures, then map the status.

        Network errors, 5xx and 429 are retried up to ``max_attempts`` total attempts.
        A 401 renewal inside ``_attempt`` does not count against this budget.
        """
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= max_attempts
            backoff = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            try:
                response = self._attempt(method, path, params)
            except TransportError as exc:
                if last:
                    raise TransportError(
                        f"{exc} (gave up after {attempt} attempts)",
                        details={"resource": path, "attempts": attempt},
                    ) from None
                self._sleep(backoff)
                continue
            status = response.status
            if status == 429:
                delay = self._parse_retry_after(response.retry_after)
                if delay is not None and delay > MAX_RETRY_AFTER_SECONDS:
                    raise RateLimitError(
                        f"Rate limited fetching {path}; Retry-After exceeds "
                        f"{MAX_RETRY_AFTER_SECONDS}s",
                        details={"status": 429, "resource": path, "attempts": attempt},
                    )
                if last:
                    raise RateLimitError(
                        f"Rate limited fetching {path} (gave up after {attempt} attempts)",
                        details={"status": 429, "resource": path, "attempts": attempt},
                    )
                self._sleep(delay if delay is not None else backoff)
                continue
            if status >= 500:
                if last:
                    raise TransportError(
                        f"Upstream error fetching {path} (HTTP {status}, "
                        f"gave up after {attempt} attempts)",
                        details={"status": status, "resource": path, "attempts": attempt},
                    )
                self._sleep(backoff)
                continue
            self._raise_for_status(response, path)
            return response

    def search_activities(
        self, start: int, limit: int, *, max_attempts: int | None = None
    ) -> ApiResponse:
        """GET one page of the activity search (SPEC §2.2), sorted by start ascending."""
        params = {
            "excludeChildren": "false",
            "sortBy": "startLocal",
            "sortOrder": "asc",
            "start": str(start),
            "limit": str(limit),
        }
        return self._request(
            "GET",
            SEARCH_PATH,
            params,
            max_attempts=DEFAULT_MAX_ATTEMPTS if max_attempts is None else max_attempts,
        )

    def get_activity(self, activity_id: str) -> ApiResponse:
        """GET the activity summary."""
        return self._request("GET", f"/activity-service/activity/{activity_id}")

    def get_activity_workouts(self, activity_id: str) -> ApiResponse:
        """GET the workouts linked to an activity."""
        return self._request("GET", f"/activity-service/activity/{activity_id}/workouts")

    def download_original_file(self, activity_id: str) -> ApiResponse:
        """GET the original activity file (typically a ZIP); bytes are returned untouched."""
        return self._request("GET", f"/download-service/files/activity/{activity_id}")


@dataclass(frozen=True)
class AuthCheckResult:
    """Outcome of a successful credential check."""

    token_obtained: bool
    search_status: int


def check_auth(client: GarminApiClient) -> AuthCheckResult:
    """Obtain a token and run one ``limit=1`` search; raises on any failure."""
    client._fetch_token()  # pyright: ignore[reportPrivateUsage]
    response = client.search_activities(0, 1)
    return AuthCheckResult(token_obtained=True, search_status=response.status)
