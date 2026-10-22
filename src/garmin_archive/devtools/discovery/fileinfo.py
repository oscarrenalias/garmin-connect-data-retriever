"""Structural analysis of original-file response bodies for the discovery tool.

Everything happens in memory: ZIP members are read through ``zipfile`` over ``BytesIO`` and
never extracted. Limit violations are recorded as findings, not raised. Only names, sizes,
counts and field names are reported, never message values.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from garmin_archive.devtools.discovery.fitsdk import decode_fit
from garmin_archive.devtools.sanitize import SanitizeContext, pseudonymize_digit_runs

MAX_ENTRIES = 64
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_COUNTED_TYPES = ("session", "lap", "length", "record")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")


def _finding(code: str, note: str) -> dict[str, str]:
    return {"code": code, "status": "confirmed", "note": note}


def _extension(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower()


def _unsafe_path(name: str) -> bool:
    if name.startswith(("/", "\\")) or PureWindowsPath(name).is_absolute():
        return True
    return ".." in name.replace("\\", "/").split("/")


def _looks_like_fit(data: bytes) -> bool:
    return len(data) >= 12 and data[8:12] == b".FIT"


def analyze_fit(data: bytes) -> dict[str, Any]:
    """Summarize decoded FIT bytes: message counts, field names per type, errors."""
    result = decode_fit(data)
    counts = {name: len(msgs) for name, msgs in result.messages.items()}
    fields: dict[str, list[str]] = {}
    for name, msgs in result.messages.items():
        seen: set[str] = set()
        for msg in msgs:
            seen.update(str(k) for k in msg)
        fields[name] = sorted(seen)
    counts_by_kind = {
        kind: counts.get(kind, counts.get(f"{kind}_mesgs", 0)) for kind in _COUNTED_TYPES
    }
    return {
        "message_counts": counts,
        "fields": fields,
        "decode_errors": list(result.errors),
        "session_count": counts_by_kind["session"],
        "lap_count": counts_by_kind["lap"],
        "length_count": counts_by_kind["length"],
        "record_count": counts_by_kind["record"],
    }


def analyze_original_file(data: bytes, ctx: SanitizeContext) -> dict[str, Any]:
    """Return a manifest dict describing ``data``. Never raises, never writes files."""
    findings: list[dict[str, str]] = []
    manifest: dict[str, Any] = {
        "format": "unknown",
        "byte_length": len(data),
        "members": [],
        "findings": findings,
    }
    if data.startswith(_ZIP_MAGIC):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            manifest["format"] = "zip"
            findings.append(_finding("zip_unreadable", f"{type(exc).__name__}"))
            return manifest
        manifest["format"] = "zip"
        with archive:
            _analyze_zip(archive, ctx, manifest)
        return manifest
    if _looks_like_fit(data):
        manifest["format"] = "fit"
        manifest["fit"] = analyze_fit(data)
    else:
        findings.append(_finding("unknown_format", "body is neither ZIP nor FIT"))
    return manifest


def _analyze_zip(archive: zipfile.ZipFile, ctx: SanitizeContext, manifest: dict[str, Any]) -> None:
    findings: list[dict[str, str]] = manifest["findings"]
    infos = archive.infolist()
    total = sum(i.file_size for i in infos)
    manifest["entry_count"] = len(infos)
    manifest["total_uncompressed_size"] = total
    within_limits = True
    if len(infos) > MAX_ENTRIES:
        within_limits = False
        findings.append(_finding("too_many_entries", f"{len(infos)} entries exceeds {MAX_ENTRIES}"))
    if total > MAX_UNCOMPRESSED_BYTES:
        within_limits = False
        findings.append(
            _finding("oversized", f"declared {total} bytes exceeds {MAX_UNCOMPRESSED_BYTES}")
        )
    members: list[dict[str, Any]] = manifest["members"]
    for index, info in enumerate(infos):
        name = pseudonymize_digit_runs(info.filename, ctx)
        unsafe = _unsafe_path(info.filename)
        if unsafe:
            within_limits = False
            findings.append(_finding("unsafe_path", f"member {index} has absolute or '..' path"))
        member: dict[str, Any] = {
            "name": name,
            "compressed_size": info.compress_size,
            "uncompressed_size": info.file_size,
            "compression_method": info.compress_type,
            "extension": _extension(info.filename),
        }
        members.append(member)
    if not within_limits:
        return
    for info, member in zip(infos, members, strict=True):
        if member["extension"] != ".fit":
            continue
        try:
            body = archive.read(info)
        except Exception as exc:  # corrupt member data, bad CRC, unsupported method
            member["fit"] = {
                "message_counts": {},
                "fields": {},
                "decode_errors": [f"{type(exc).__name__}: member unreadable"],
                "session_count": 0,
                "lap_count": 0,
                "length_count": 0,
                "record_count": 0,
            }
            continue
        member["fit"] = analyze_fit(body)
