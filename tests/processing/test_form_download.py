from __future__ import annotations

import logging

import httpx
import pytest

from garmin_archive.processing.form_download import (
    MAX_DOWNLOAD_BYTES,
    FormDownloadError,
    download_form_zip,
)

HOST = "formdata-prod.s3.eu-west-1.amazonaws.com"
URL = f"https://{HOST}/export.zip?X-Amz-Signature=SECRET123"


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_returns_zip_bytes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"PK-zip")

    assert download_form_zip(_client(httpx.MockTransport(handler)), URL) == b"PK-zip"
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    "url",
    [
        "http://" + HOST + "/a.zip?sig=SECRET123",
        "https://evil.example.com/a.zip?sig=SECRET123",
        "https://formdata-x.s3.eu-west-1.amazonaws.com.evil.com/a.zip?sig=SECRET123",
        "https://user@evil.com/a.zip?sig=SECRET123",
    ],
)
def test_refused_urls_make_no_request_and_hide_query(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(FormDownloadError) as info:
        download_form_zip(_client(httpx.MockTransport(handler)), url)
    assert "SECRET123" not in str(info.value)
    assert "sig=" not in str(info.value)


def test_redirect_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/x?s=SECRET123"})

    with pytest.raises(FormDownloadError) as info:
        download_form_zip(_client(httpx.MockTransport(handler)), URL)
    assert HOST in str(info.value)
    assert "SECRET123" not in str(info.value)


def test_oversize_body_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_DOWNLOAD_BYTES + 1))

    with pytest.raises(FormDownloadError, match="size cap"):
        download_form_zip(_client(httpx.MockTransport(handler)), URL)


def test_streamed_oversize_without_content_length_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([b"x" * 600, b"x" * 600]))

    with pytest.raises(FormDownloadError, match="size cap"):
        download_form_zip(_client(httpx.MockTransport(handler)), URL, max_bytes=1000)


def test_http_error_status_and_transport_errors_hide_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout {request.url}")

    with caplog.at_level(logging.DEBUG):
        for handler in (forbidden, boom, slow):
            with pytest.raises(FormDownloadError) as info:
                download_form_zip(_client(httpx.MockTransport(handler)), URL)
            assert HOST in str(info.value)
            assert "SECRET123" not in str(info.value)
            assert info.value.__cause__ is None
    assert "SECRET123" not in caplog.text


def test_explicit_port_url_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    url = f"https://{HOST}:443/export.zip?X-Amz-Signature=SECRET123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PK-zip")

    with caplog.at_level(logging.DEBUG):
        assert download_form_zip(_client(httpx.MockTransport(handler)), url) == b"PK-zip"
    assert "SECRET123" not in caplog.text
