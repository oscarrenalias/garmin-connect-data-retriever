"""Validate and import FORM goggles FIT files (local ``.fit``/``.zip`` files or ZIP bytes).

Rejection reasons are fixed strings: they never include FIT field values such as the serial
number.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import httpx
from garmin_fit_sdk import Decoder, Stream

from garmin_archive.archive import ArchiveWriter
from garmin_archive.models import Clock
from garmin_archive.processing.fitfile import FitUnavailableError, zip_member_contents
from garmin_archive.processing.form import rebuild_form
from garmin_archive.processing.form_download import FormDownloadError, download_form_zip

FORM_MANUFACTURER = "form"


@dataclass(frozen=True)
class RejectedFile:
    """A file or ZIP member that was not imported, with the reason."""

    file: str
    reason: str


@dataclass
class FormImportReport:
    """Counts for one import run."""

    imported: int = 0
    duplicate: int = 0
    rejected: list[RejectedFile] = field(default_factory=lambda: [])


def _check_form_fit(body: bytes) -> str | None:
    """Return a rejection reason, or ``None`` when ``body`` is a decodable FORM FIT file."""
    try:
        decoder = Decoder(Stream.from_byte_array(bytearray(body)))
        if not decoder.is_fit():
            return "not a FIT file"
        raw, errors = decoder.read()
    except Exception:
        return "FIT file could not be decoded"
    if errors:
        return "FIT file could not be decoded"
    messages = cast("dict[str, list[dict[str, Any]]]", raw)
    file_ids = messages.get("file_id_mesgs", [])
    if not file_ids:
        return "FIT file has no file_id message"
    if file_ids[0].get("manufacturer") != FORM_MANUFACTURER:
        return "FIT file was not produced by FORM"
    return None


def _import_body(writer: ArchiveWriter, report: FormImportReport, body: bytes, name: str) -> None:
    reason = _check_form_fit(body)
    if reason is not None:
        report.rejected.append(RejectedFile(name, reason))
        return
    result = writer.record_import(body=body, filename=name)
    if result.is_duplicate:
        report.duplicate += 1
    else:
        report.imported += 1


def _import_zip(writer: ArchiveWriter, report: FormImportReport, body: bytes, name: str) -> None:
    try:
        members = zip_member_contents(body)
    except FitUnavailableError as exc:
        report.rejected.append(RejectedFile(name, str(exc)))
        return
    fit_members = 0
    for member, data in members.items():
        label = f"{name}:{member}"
        if not member.lower().endswith(".fit"):
            report.rejected.append(RejectedFile(label, "ZIP member is not a .fit file"))
            continue
        fit_members += 1
        _import_body(writer, report, data, label)
    if fit_members == 0 and not members:
        report.rejected.append(RejectedFile(name, "ZIP has no .fit member"))


def import_form_zip_bytes(
    writer: ArchiveWriter, body: bytes, *, name: str = "download.zip"
) -> FormImportReport:
    """Import every FORM FIT file in the in-memory ZIP ``body``."""
    report = FormImportReport()
    _import_zip(writer, report, body, name)
    return report


def import_form_files(writer: ArchiveWriter, paths: Iterable[Path]) -> FormImportReport:
    """Import local ``.fit`` and ``.zip`` files; other files and unreadable paths are rejected."""
    report = FormImportReport()
    for path in paths:
        name = path.name
        suffix = path.suffix.lower()
        if suffix not in (".fit", ".zip"):
            report.rejected.append(RejectedFile(name, "unsupported file type"))
            continue
        try:
            body = path.read_bytes()
        except OSError:
            report.rejected.append(RejectedFile(name, "file could not be read"))
            continue
        if suffix == ".zip":
            _import_zip(writer, report, body, name)
        else:
            _import_body(writer, report, body, name)
    return report


class FormImportOutcome(StrEnum):
    """Result class the CLI maps to exit codes: 0, 6, 2 and 4."""

    OK = "ok"
    PARTIAL = "partial"
    NOTHING_VALID = "nothing_valid"
    DOWNLOAD_FAILED = "download_failed"


@dataclass
class FormImportResult:
    """Output of :func:`import_form`; never contains the download URL (only its host, in errors)."""

    outcome: FormImportOutcome
    imported: int = 0
    duplicate: int = 0
    rejected: list[RejectedFile] = field(default_factory=lambda: [])
    matched: int = 0
    unmatched: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON shape: imported, duplicate, rejected [{file, reason}], matched, unmatched."""
        data: dict[str, Any] = {
            "imported": self.imported,
            "duplicate": self.duplicate,
            "rejected": [{"file": r.file, "reason": r.reason} for r in self.rejected],
            "matched": self.matched,
            "unmatched": self.unmatched,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


def import_form(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    paths: Sequence[Path] | None = None,
    url: str | None = None,
    http_client: httpx.Client | None = None,
) -> FormImportResult:
    """Import FORM FIT files from local ``paths`` or a download ``url``, then rebuild FORM rows.

    Exactly one of ``paths`` and ``url`` must be given; a ``url`` needs ``http_client``. A failed
    download returns ``DOWNLOAD_FAILED`` with a host-only error and imports nothing. The rebuild
    is offline and runs whenever at least one file was valid (imported or duplicate).
    """
    if (paths is None) == (url is None):
        raise ValueError("provide exactly one of paths or url")
    writer = ArchiveWriter(conn, clock)
    if url is not None:
        if http_client is None:
            raise ValueError("http_client is required when url is given")
        try:
            body = download_form_zip(http_client, url)
        except FormDownloadError as exc:
            return FormImportResult(FormImportOutcome.DOWNLOAD_FAILED, error=str(exc))
        report = import_form_zip_bytes(writer, body)
    else:
        assert paths is not None
        report = import_form_files(writer, paths)

    result = FormImportResult(
        FormImportOutcome.NOTHING_VALID,
        imported=report.imported,
        duplicate=report.duplicate,
        rejected=report.rejected,
    )
    if report.imported + report.duplicate == 0:
        return result
    rebuilt = rebuild_form(conn)
    result.matched = rebuilt.matched
    result.unmatched = rebuilt.unmatched
    result.outcome = FormImportOutcome.PARTIAL if report.rejected else FormImportOutcome.OK
    return result
