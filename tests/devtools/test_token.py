from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from garmin_archive.devtools.discovery.credentials import SecretStr
from garmin_archive.devtools.discovery.http import DiscoveryHttp
from garmin_archive.devtools.discovery.token import (
    SEARCH_PATH,
    TOKEN_PATH,
    acquire_token,
    auto_token_field,
    describe_token_body,
)

API_KEY = "api-key-SECRET-1"
TOKEN = "tok.en.SECRET-2"


def run(
    token_response: httpx.Response,
    probe_status: int = 200,
    token_field: str | None = None,
    requests: list[httpx.Request] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path == TOKEN_PATH:
            return token_response
        assert request.url.path == SEARCH_PATH
        return httpx.Response(probe_status, json=[])

    http = DiscoveryHttp(delay=0, sleep=lambda s: None, transport=httpx.MockTransport(handler))
    return acquire_token(http, SecretStr(API_KEY), token_field)


def test_auto_selection_with_expiry() -> None:
    reqs: list[httpx.Request] = []
    body = {"accessToken": TOKEN, "expiresIn": 3600}
    result = run(httpx.Response(200, json=body), requests=reqs)
    assert result.outcome is None
    assert result.token.get_secret_value() == TOKEN
    assert result.token_report["token_field"] == "accessToken"
    assert result.token_report["token_field_source"] == "auto"
    assert result.token_report["status"] == "confirmed"
    assert result.token_report["expiry_fields"] == {"expiresIn": 3600}
    assert result.header_convention == {
        "scheme": "Bearer",
        "probe_http_status": 200,
        "status": "confirmed",
    }
    assert reqs[0].method == "POST"
    assert reqs[0].headers["Authorization"] == f"ApiKey {API_KEY}"
    assert reqs[1].headers["Authorization"] == f"Bearer {TOKEN}"
    assert reqs[1].url.params["limit"] == "1"


def test_ambiguous_needs_token_field_then_explicit() -> None:
    body = {"token": TOKEN, "idToken": "id.tok.en"}
    result = run(httpx.Response(200, json=body))
    assert result.outcome == "needs_token_field"
    assert result.token is None
    assert result.token_report["status"] == "unknown"
    assert result.header_convention == {}
    explicit = run(httpx.Response(200, json=body), token_field="idToken")
    assert explicit.outcome is None
    assert explicit.token_report["token_field"] == "idToken"
    assert explicit.token_report["token_field_source"] == "explicit"
    assert explicit.token.get_secret_value() == "id.tok.en"


def test_explicit_field_missing_needs_token_field() -> None:
    result = run(httpx.Response(200, json={"accessToken": TOKEN}), token_field="nope")
    assert result.outcome == "needs_token_field"


def test_auto_excludes_refresh_and_type_keys() -> None:
    assert auto_token_field(["accessToken", "refreshToken", "tokenType"]) == "accessToken"
    assert auto_token_field(["refreshToken", "tokenType"]) is None
    assert auto_token_field(["a", "b"]) is None


def test_probe_401_and_403_stop() -> None:
    for status in (401, 403):
        result = run(httpx.Response(200, json={"accessToken": TOKEN}), probe_status=status)
        assert result.outcome == "stopped"
        assert result.header_convention["probe_http_status"] == status
        assert result.header_convention["status"] == "failed"


def test_probe_non_2xx_non_auth_is_unknown() -> None:
    result = run(httpx.Response(200, json={"accessToken": TOKEN}), probe_status=404)
    assert result.outcome is None
    assert result.header_convention["status"] == "unknown"


def test_non_2xx_token_response_records_status_and_content_type_only() -> None:
    resp = httpx.Response(
        401, json={"message": "bad key SECRET"}, headers={"X-Extra": "SECRET-HDR"}
    )
    result = run(resp)
    assert result.outcome == "stopped"
    assert result.token_report == {
        "http_status": 401,
        "content_type": "application/json",
        "status": "failed",
    }


def test_non_json_body_records_length() -> None:
    result = run(httpx.Response(200, content=b"not json at all"))
    assert result.outcome == "needs_token_field"
    assert result.token_report["body_length"] == 15
    assert "fields" not in result.token_report


def test_retries_exhausted_on_token_stops() -> None:
    result = run(httpx.Response(503))
    assert result.outcome == "stopped"
    assert result.token_report["status"] == "failed"


def test_report_never_contains_secrets() -> None:
    body = {"accessToken": TOKEN, "refreshToken": "refresh-SECRET-3", "expiresIn": 10}
    result = run(httpx.Response(200, json=body))
    dumped = json.dumps([result.token_report, result.header_convention, result.note])
    for secret in (API_KEY, TOKEN, "refresh-SECRET-3"):
        assert secret not in dumped
    assert TOKEN not in repr(result)
    assert result.token_report["fields"]["accessToken"] == {
        "type": "string",
        "length": len(TOKEN),
        "jwt_like": True,
    }


def test_describe_token_body_non_object() -> None:
    assert describe_token_body(b"[1,2]")[3] == 5
    fields, expiry, strings, length = describe_token_body(
        b'{"a": true, "ttl": 5, "expired": false, "t": "x"}'
    )
    assert length is None
    assert expiry == {"ttl": 5}
    assert fields["a"] == {"type": "boolean"}
    assert strings == {"t": "x"}


def _b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _jwt() -> str:
    header = _b64({"alg": "RS256", "typ": "JWT", "kid": "KID-SECRET"})
    payload = _b64({"iss": "ISS-SECRET", "sub": "SUB-SECRET", "iat": 1000, "exp": 4600})
    return f"{header}.{payload}.c2ln-SECRET"


def _text(body: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/plain"})


def test_raw_body_jwt_token_is_used_and_described_without_values() -> None:
    jwt = _jwt()
    reqs: list[httpx.Request] = []
    result = run(_text(jwt), requests=reqs)
    assert result.outcome is None
    assert result.token.get_secret_value() == jwt
    report = result.token_report
    assert report["token_field"] is None
    assert report["token_field_source"] == "raw_body"
    assert report["status"] == "confirmed"
    assert report["body_length"] == len(jwt)
    assert report["jwt_like"] is True
    assert report["jwt"] == {
        "header_keys": ["alg", "kid", "typ"],
        "alg": "RS256",
        "claim_names": ["exp", "iat", "iss", "sub"],
        "lifetime_s": 3600,
    }
    assert result.header_convention["status"] == "confirmed"
    assert reqs[1].headers["Authorization"] == f"Bearer {jwt}"
    dumped = json.dumps([report, result.header_convention, result.note])
    for secret in (jwt, "SECRET", "RS256-x"):
        assert secret not in dumped


def test_raw_body_is_stripped_and_non_jwt_has_no_jwt_section() -> None:
    result = run(_text("  plain-token_1.2\n"))
    assert result.outcome is None
    assert result.token.get_secret_value() == "plain-token_1.2"
    assert result.token_report["jwt_like"] is False
    assert "jwt" not in result.token_report


def test_raw_body_undecodable_jwt_records_decode_error() -> None:
    result = run(_text("aaa.bbb.ccc"))
    assert result.outcome is None
    assert result.token_report["jwt"] == {"decode_error": True}


def test_raw_body_with_inner_space_or_newline_needs_token_field() -> None:
    for body in ("abc def", "abc\ndef", "", "   ", "a+b/c=", '"quoted"'):
        result = run(_text(body))
        assert result.outcome == "needs_token_field", body
        assert result.token is None
