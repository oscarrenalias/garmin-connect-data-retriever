"""Safe in-memory extraction of the FIT file from an ``original_file`` ZIP payload."""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath, PureWindowsPath

from garmin_archive.models import GarminArchiveError

MAX_ZIP_ENTRIES = 20
MAX_FIT_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024


class FitUnavailableError(GarminArchiveError):
    """The FIT file could not be safely extracted from the payload."""

    code = "FIT_UNAVAILABLE"


def _is_unsafe(name: str) -> bool:
    if not name or "\x00" in name:
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or PureWindowsPath(name).is_absolute():
        return True
    if PureWindowsPath(name).drive:
        return True
    return ".." in PurePosixPath(normalized).parts


def extract_fit(body: bytes) -> bytes:
    """Return the bytes of the first ``.fit`` member of the ZIP ``body``."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise FitUnavailableError("original_file payload is not a valid ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise FitUnavailableError("ZIP has too many entries")
        if any(_is_unsafe(info.filename) for info in infos):
            raise FitUnavailableError("ZIP contains an unsafe member path")
        if sum(info.file_size for info in infos) > MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES:
            raise FitUnavailableError("ZIP uncompressed size exceeds limit")
        for info in infos:
            if info.is_dir() or not info.filename.lower().endswith(".fit"):
                continue
            if info.file_size > MAX_FIT_UNCOMPRESSED_BYTES:
                raise FitUnavailableError("FIT member exceeds size limit")
            try:
                # Bound the read so a lying header cannot inflate past the limit.
                with archive.open(info) as member:
                    data = member.read(MAX_FIT_UNCOMPRESSED_BYTES + 1)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as exc:
                raise FitUnavailableError("FIT member could not be read") from exc
            if len(data) > MAX_FIT_UNCOMPRESSED_BYTES:
                raise FitUnavailableError("FIT member exceeds size limit")
            return data
    raise FitUnavailableError("ZIP has no .fit member")
