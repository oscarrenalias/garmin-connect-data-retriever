"""Cross-cutting check: the fake key and token never leak (spec-e4f45b59 AC 9)."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from garmin_archive.api import TOKEN_PATH, GarminApiClient
from garmin_archive.cli import main
from garmin_archive.credentials import require_api_key
from garmin_archive.models import GarminArchiveError

KEY = "leak-test-api-key-9f3a"
TOKEN = "leak-test-bearer-token-71bc"
SECRETS = (KEY, TOKEN)

Handler = Callable[[httpx.Request], httpx.Response]


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def _ok_token(data: Handler) -> Handler:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == TOKEN_PATH:
            return httpx.Response(200, text=TOKEN)
        return data(req)

    return handler


def _client(handler: Handler) -> GarminApiClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return GarminApiClient(KEY, http_client=http, sleep=lambda _s: None)


SCENARIOS: dict[str, Handler] = {
    "token-401": lambda r: httpx.Response(401, text="denied"),
    "token-403": lambda r: httpx.Response(403, text="denied"),
    "double-401": _ok_token(lambda r: httpx.Response(401)),
    "forbidden": _ok_token(lambda r: httpx.Response(403)),
    "redirect": _ok_token(lambda r: httpx.Response(302, headers={"location": "https://x.test/"})),
    "bad-request": _ok_token(lambda r: httpx.Response(400)),
    "persistent-500": _ok_token(lambda r: httpx.Response(500)),
    "rate-limit": _ok_token(lambda r: httpx.Response(429, headers={"Retry-After": "600"})),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_client_errors_never_leak(
    name: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(GarminArchiveError) as ei:
        _client(SCENARIOS[name]).search_activities(0, 1)
    exc = ei.value
    _assert_clean(str(exc))
    _assert_clean(repr(exc))
    _assert_clean("".join(traceback.format_exception(exc)))
    _assert_clean(caplog.text)
    out = capsys.readouterr()
    _assert_clean(out.out + out.err)


def test_success_path_never_leaks(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client(_ok_token(lambda r: httpx.Response(200, text="[]")))
    client.search_activities(0, 1)
    _assert_clean(caplog.text)
    _assert_clean(repr(client))
    out = capsys.readouterr()
    _assert_clean(out.out + out.err)


def test_credential_source_never_leaks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"GARMIN_API_KEY={KEY}\n", encoding="utf-8")
    source = require_api_key({}, env)
    for text in (repr(source), str(source), source.origin):
        _assert_clean(text)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_cli_check_never_leaks(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GARMIN_API_KEY", KEY)
    monkeypatch.setattr("time.sleep", lambda s: None)
    caplog.set_level(logging.DEBUG)
    http = httpx.Client(transport=httpx.MockTransport(SCENARIOS[name]), follow_redirects=False)
    code = main(["--env-file", str(tmp_path / "x.env"), "auth", "check"], http_client=http)
    assert code != 0
    out = capsys.readouterr()
    _assert_clean(out.out + out.err)
    _assert_clean(caplog.text)
