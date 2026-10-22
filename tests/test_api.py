import httpx
import pytest

from garmin_archive.api import (
    TOKEN_PATH,
    ApiResponse,
    GarminApiClient,
    default_http_client,
)
from garmin_archive.models import (
    AuthenticationFailedError,
    AuthorizationError,
    ContractError,
)

KEY = "secret-key-123"


def make_client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return GarminApiClient(KEY, http_client=http, sleep=lambda _s: None)


def test_token_used_stripped_and_fetched_once():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == TOKEN_PATH:
            assert req.method == "POST"
            assert req.headers["Authorization"] == f"ApiKey {KEY}"
            return httpx.Response(200, text="tok-1\n", headers={"content-type": "text/plain"})
        assert req.headers["Authorization"] == "Bearer tok-1"
        return httpx.Response(
            200, content=b"{ not json", headers={"content-type": "application/json"}
        )

    client = make_client(handler)
    r1 = client._request("GET", "/a")
    client._request("GET", "/b", {"x": "1"})
    assert r1 == ApiResponse(200, "application/json", b"{ not json")
    assert sum(1 for r in seen if r.url.path == TOKEN_PATH) == 1


def test_401_then_200_renews_once():
    tokens = iter(["t1", "t2"])
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == TOKEN_PATH:
            calls["token"] += 1
            return httpx.Response(200, text=next(tokens))
        if req.headers["Authorization"] == "Bearer t1":
            return httpx.Response(401)
        return httpx.Response(200, content=b"ok")

    assert make_client(handler)._request("GET", "/a").body == b"ok"
    assert calls["token"] == 2


def test_two_401s_raise_authentication_failed():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == TOKEN_PATH:
            return httpx.Response(200, text="t")
        return httpx.Response(401)

    with pytest.raises(AuthenticationFailedError) as ei:
        make_client(handler)._request("GET", "/a")
    assert KEY not in str(ei.value) and "Bearer" not in str(ei.value)


@pytest.mark.parametrize("status", [401, 403])
def test_token_endpoint_rejection_not_retried(status):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status, text="denied")

    with pytest.raises(AuthenticationFailedError) as ei:
        make_client(handler)._request("GET", "/a")
    assert len(calls) == 1
    assert KEY not in str(ei.value)


def test_status_mapping():
    def run(status: int, headers: dict[str, str] | None = None):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == TOKEN_PATH:
                return httpx.Response(200, text="t")
            return httpx.Response(status, headers=headers or {}, content=b"body-secret")

        return make_client(handler)._request("GET", "/a")

    assert run(404).status == 404
    assert run(204).status == 204
    with pytest.raises(AuthorizationError):
        run(403)
    for status in (400, 410, 301, 302, 304):
        with pytest.raises(ContractError) as ei:
            run(status, {"location": "https://evil.example/x"})
        assert "body-secret" not in str(ei.value)


def test_redirect_to_other_host_not_followed():
    hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        if req.url.path == TOKEN_PATH:
            return httpx.Response(200, text="t")
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    with pytest.raises(ContractError):
        make_client(handler)._request("GET", "/a")
    assert "evil.example" not in hosts


def test_default_client_settings():
    http = default_http_client()
    try:
        assert http.follow_redirects is False
        t = http.timeout
        assert None not in (t.connect, t.read, t.write, t.pool)
    finally:
        http.close()
