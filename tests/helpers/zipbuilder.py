"""Build ZIP archives in memory."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping


def build_zip(members: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()
