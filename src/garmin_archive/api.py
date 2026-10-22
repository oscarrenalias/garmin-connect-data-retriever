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
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


@dataclass(frozen=True)
class ApiResponse:
    """A raw HTTP response; the body is never parsed."""

    status: int
    content_type: str | None
    body: bytes


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

    def _request(
        self, method: str, path: str, params: Mapping[str, str] | None = None
    ) -> ApiResponse:
        """Perform a data request and map the status; retry loop wraps ``_attempt``."""
        response = self._attempt(method, path, params)
        self._raise_for_status(response, path)
        return response
