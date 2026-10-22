"""Download one FORM export ZIP from a pre-signed S3 URL.

The URL is a credential: it is never logged and never placed in an error message or detail;
only the host is. No Garmin credentials are involved.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from garmin_archive.models import GarminArchiveError

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_HOST_PATTERN = re.compile(r"^formdata-[a-z0-9-]+\.s3\.[a-z0-9-]+\.amazonaws\.com$")


class FormDownloadError(GarminArchiveError):
    """A FORM download was refused or failed. The message holds the host only."""

    code = "FORM_DOWNLOAD_ERROR"


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or "unknown-host"
    except ValueError:
        return "unknown-host"


def download_form_zip(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> bytes:
    """Fetch the ZIP at ``url`` through ``client`` and return its bytes.

    Requires ``https`` and an allow-listed FORM S3 host, never follows redirects (any 3xx is an
    error) and refuses bodies over ``max_bytes``.
    """
    host = _host_of(url)
    try:
        parts = urlsplit(url)
        scheme = parts.scheme
        parsed_host = parts.hostname
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise FormDownloadError(f"Invalid download URL for host {host}") from None
    if scheme != "https":
        raise FormDownloadError(f"Download refused for host {host}: https is required")
    if parsed_host is None or not _HOST_PATTERN.fullmatch(parsed_host):
        raise FormDownloadError(f"Download refused: host {host} is not an allowed FORM host")

    chunks: list[bytes] = []
    total = 0
    try:
        with client.stream(
            "GET", url, follow_redirects=False, timeout=DOWNLOAD_TIMEOUT
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                raise FormDownloadError(f"Download from {host} refused: redirect (HTTP {status})")
            if status != 200:
                raise FormDownloadError(
                    f"Download from {host} failed: HTTP {status}",
                    retryable=status >= 500,
                )
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise FormDownloadError(f"Download from {host} refused: body exceeds size cap")
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FormDownloadError(f"Download from {host} refused: body exceeds size cap")
                chunks.append(chunk)
    except FormDownloadError:
        raise
    except httpx.TimeoutException:
        raise FormDownloadError(f"Download from {host} timed out", retryable=True) from None
    except httpx.HTTPError:
        raise FormDownloadError(f"Download from {host} failed", retryable=True) from None
    return b"".join(chunks)
