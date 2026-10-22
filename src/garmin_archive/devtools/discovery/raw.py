"""Raw capture recorder for the discovery tool.

Writes every data-call response body byte-for-byte to `<base>/<run-id>/raw/` and appends one
line per call to `requests.jsonl`. Only `HttpResponse` objects from data calls are accepted;
the token endpoint response and auth headers never reach this module. Header values are
filtered through the allowlist again here as defence in depth.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from garmin_archive.devtools.discovery.http import HttpResponse, RetriesExhausted, is_allowlisted

DEFAULT_BASE_DIR = Path(".local") / "discovery"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
REQUESTS_LOG = "requests.jsonl"

_EXTENSIONS: dict[str, str] = {
    "application/json": "json",
    "application/zip": "zip",
    "application/gpx+xml": "gpx",
    "application/vnd.garmin.tcx+xml": "tcx",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "txt",
    "text/html": "html",
}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def format_run_id(moment: datetime) -> str:
    """Format an aware datetime as a UTC run id (YYYYMMDDTHHMMSSZ)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(RUN_ID_FORMAT)


def _extension(headers: Mapping[str, str]) -> str:
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in _EXTENSIONS:
        return _EXTENSIONS[content_type]
    if content_type.endswith("+json"):
        return "json"
    if content_type.endswith("+xml"):
        return "xml"
    return "bin"


class RawRecorder:
    """Records data-call responses for one discovery run."""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.run_id = format_run_id(clock())
        self.run_dir = (base_dir if base_dir is not None else DEFAULT_BASE_DIR) / self.run_id
        self.raw_dir = self.run_dir / "raw"
        self.requests_path = self.run_dir / REQUESTS_LOG
        self._seq = 0
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        label: str,
        method: str,
        path: str,
        params: Mapping[str, str] | None,
        response: HttpResponse,
    ) -> Path:
        """Save the body and log the call. Returns the body file path.

        The file name is `<seq:04d>-<label>.<ext>`, so it is deterministic for a given call
        order and label. `label` is sanitized to a filesystem-safe token.
        """
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        safe_label = _UNSAFE.sub("_", label).strip("._") or "response"
        headers = {k: v for k, v in response.headers.values.items() if is_allowlisted(k)}
        body_path = self.raw_dir / f"{self._seq:04d}-{safe_label}.{_extension(headers)}"
        body_path.write_bytes(response.body)
        entry = {
            "seq": self._seq,
            "method": method.upper(),
            "path": path,
            "params": dict(params) if params else {},
            "status": response.status,
            "headers": headers,
            "elapsed_ms": response.elapsed_ms,
            "body_file": f"raw/{body_path.name}",
            "body_bytes": len(response.body),
        }
        self._append(entry)
        return body_path

    def record_failure(
        self,
        label: str,
        method: str,
        path: str,
        params: Mapping[str, str] | None,
        failure: RetriesExhausted,
    ) -> None:
        """Log a data call that ended in a 5xx or transport error. No body is stored."""
        self._seq += 1
        entry: dict[str, object] = {
            "seq": self._seq,
            "label": _UNSAFE.sub("_", label).strip("._") or "response",
            "method": method.upper(),
            "path": path,
            "params": dict(params) if params else {},
            "status": failure.last_status,
            "error": failure.error,
            "attempts": failure.attempts,
            "headers": {},
            "elapsed_ms": failure.elapsed_ms,
        }
        self._append(entry)

    def _append(self, entry: Mapping[str, object]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.requests_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
