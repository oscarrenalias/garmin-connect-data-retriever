"""Validate and import FORM goggles FIT files (local ``.fit``/``.zip`` files or ZIP bytes).

Rejection reasons are fixed strings: they never include FIT field values such as the serial
number.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from garmin_fit_sdk import Decoder, Stream

from garmin_archive.archive import ArchiveWriter
from garmin_archive.processing.fitfile import FitUnavailableError, zip_member_contents

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
