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
    RateLimitError,
    TransportError,
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


def make_recording_client(handler):
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return GarminApiClient(KEY, http_client=http, sleep=sleeps.append), sleeps


def _data_handler(data_responses, calls):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == TOKEN_PATH:
            return httpx.Response(200, text="tok")
        calls.append(req)
        item = data_responses[min(len(calls), len(data_responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def test_persistent_500_makes_five_attempts():
    calls: list[httpx.Request] = []
    client, sleeps = make_recording_client(_data_handler([httpx.Response(500)], calls))
    with pytest.raises(TransportError):
        client._request("GET", "/a")
    assert len(calls) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_network_error_retried_then_transport_error():
    calls: list[httpx.Request] = []
    client, sleeps = make_recording_client(_data_handler([httpx.ConnectError("boom")], calls))
    with pytest.raises(TransportError):
        client._request("GET", "/a")
    assert len(calls) == 5
    assert len(sleeps) == 4


def test_max_attempts_parameter():
    calls: list[httpx.Request] = []
    client, _ = make_recording_client(_data_handler([httpx.Response(503)], calls))
    with pytest.raises(TransportError):
        client._request("GET", "/a", max_attempts=2)
    assert len(calls) == 2


def test_retry_after_seconds_slept_then_succeeds():
    calls: list[httpx.Request] = []
    responses = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200)]
    client, sleeps = make_recording_client(_data_handler(responses, calls))
    assert client._request("GET", "/a").status == 200
    assert sleeps == [2]


def test_retry_after_too_long_raises_without_sleeping():
    calls: list[httpx.Request] = []
    responses = [httpx.Response(429, headers={"Retry-After": "600"})]
    client, sleeps = make_recording_client(_data_handler(responses, calls))
    with pytest.raises(RateLimitError):
        client._request("GET", "/a")
    assert sleeps == []
    assert len(calls) == 1


def test_persistent_429_raises_rate_limit_after_exhaustion():
    calls: list[httpx.Request] = []
    client, _ = make_recording_client(_data_handler([httpx.Response(429)], calls))
    with pytest.raises(RateLimitError):
        client._request("GET", "/a")
    assert len(calls) == 5


def test_401_renewal_does_not_consume_retry_budget():
    calls: list[httpx.Request] = []
    responses = [httpx.Response(401), httpx.Response(500), httpx.Response(200)]
    client, sleeps = make_recording_client(_data_handler(responses, calls))
    assert client._request("GET", "/a").status == 200
    assert sleeps == [1.0]


def test_search_sends_exactly_fixed_params():
    calls: list[httpx.Request] = []
    client, _ = make_recording_client(_data_handler([httpx.Response(200, text="[]")], calls))
    resp = client.search_activities(20, 10)
    assert resp.body == b"[]"
    assert calls[0].url.path == "/activitylist-service/activities/search/activities"
    assert dict(calls[0].url.params) == {
        "excludeChildren": "false",
        "sortBy": "startLocal",
        "sortOrder": "asc",
        "start": "20",
        "limit": "10",
    }


def test_search_max_attempts_override():
    calls: list[httpx.Request] = []
    client, _ = make_recording_client(_data_handler([httpx.Response(500)], calls))
    with pytest.raises(TransportError):
        client.search_activities(0, 10, max_attempts=2)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get_activity", "/activity-service/activity/123"),
        ("get_activity_workouts", "/activity-service/activity/123/workouts"),
        ("download_original_file", "/download-service/files/activity/123"),
    ],
)
def test_per_activity_paths(method, path):
    calls: list[httpx.Request] = []
    client, _ = make_recording_client(_data_handler([httpx.Response(200, text="{}")], calls))
    getattr(client, method)("123")
    assert calls[0].method == "GET"
    assert calls[0].url.path == path
    assert not calls[0].url.query


def test_zip_body_returned_byte_identical():
    payload = b"PK\x03\x04\x00\xff\xfe binary \x00\x80"
    calls: list[httpx.Request] = []
    resp_in = httpx.Response(200, content=payload, headers={"Content-Type": "application/zip"})
    client, _ = make_recording_client(_data_handler([resp_in], calls))
    resp = client.download_original_file("9")
    assert resp.body == payload
    assert resp.content_type == "application/zip"
