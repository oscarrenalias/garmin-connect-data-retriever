from __future__ import annotations

import random
import struct
from pathlib import Path

from garmin_archive.devtools.discovery.fileinfo import analyze_original_file
from garmin_archive.devtools.sanitize import SanitizeContext
from helpers.fitbuilder import build_fit
from helpers.zipbuilder import build_zip


def _ctx() -> SanitizeContext:
    return SanitizeContext(rng=random.Random(1))


def _codes(manifest: dict[str, object]) -> list[str]:
    findings = manifest["findings"]
    assert isinstance(findings, list)
    return [f["code"] for f in findings]  # pyright: ignore[reportUnknownVariableType]


def test_valid_fit_message_counts() -> None:
    manifest = analyze_original_file(build_fit(record_count=3), _ctx())
    assert manifest["format"] == "fit"
    fit = manifest["fit"]
    assert fit["decode_errors"] == []
    assert fit["record_count"] == 3
    assert fit["session_count"] == 0
    counts = fit["message_counts"]
    assert sum(counts.values()) == 4
    assert "heart_rate" in next(iter(fit["fields"][k] for k in fit["fields"] if "record" in k))


def test_fit_inside_zip(tmp_path: Path) -> None:
    data = build_zip({"activity.fit": build_fit(2)})
    manifest = analyze_original_file(data, _ctx())
    assert manifest["format"] == "zip"
    assert manifest["entry_count"] == 1
    assert manifest["members"][0]["fit"]["record_count"] == 2
    assert manifest["findings"] == []
    assert list(tmp_path.iterdir()) == []


def test_dotdot_member_is_finding_without_raise_or_writes(tmp_path: Path) -> None:
    data = build_zip({"../evil.fit": build_fit(1), "ok.txt": b"x"})
    manifest = analyze_original_file(data, _ctx())
    assert "unsafe_path" in _codes(manifest)
    assert all("fit" not in m for m in manifest["members"])
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path.parent / "evil.fit").exists()


def test_absolute_member_is_finding() -> None:
    manifest = analyze_original_file(build_zip({"/abs/x.fit": b"x"}), _ctx())
    assert "unsafe_path" in _codes(manifest)


def test_too_many_entries() -> None:
    data = build_zip({f"f{i}.txt": b"a" for i in range(65)})
    manifest = analyze_original_file(data, _ctx())
    assert "too_many_entries" in _codes(manifest)
    assert manifest["entry_count"] == 65
    assert len(manifest["members"]) == 65


def test_exactly_64_entries_ok() -> None:
    data = build_zip({f"f{i}.txt": b"a" for i in range(64)})
    assert analyze_original_file(data, _ctx())["findings"] == []


def _forge_size(data: bytes, size: int) -> bytes:
    """Overwrite the uncompressed-size field in the central directory header."""
    buf = bytearray(data)
    pos = buf.index(b"PK\x01\x02")
    struct.pack_into("<I", buf, pos + 24, size)
    return bytes(buf)


def test_oversized_declared_size() -> None:
    data = _forge_size(build_zip({"a.fit": build_fit(1)}), 300 * 1024 * 1024)
    manifest = analyze_original_file(data, _ctx())
    assert "oversized" in _codes(manifest)
    assert "fit" not in manifest["members"][0]


def test_corrupt_fit_member_decode_errors() -> None:
    bad = b"\x0e\x20\x00\x00\x10\x00\x00\x00.FIT\x00\x00" + b"\xff" * 20
    manifest = analyze_original_file(build_zip({"a.fit": bad}), _ctx())
    assert manifest["members"][0]["fit"]["decode_errors"]


def test_corrupt_bare_fit_decode_errors() -> None:
    data = bytearray(build_fit(2))
    data[20:24] = b"\xff\xff\xff\xff"
    data[-1] ^= 0xFF
    manifest = analyze_original_file(bytes(data), _ctx())
    assert manifest["format"] == "fit"
    assert manifest["fit"]["decode_errors"]


def test_member_name_digit_runs_pseudonymized() -> None:
    ctx = _ctx()
    manifest = analyze_original_file(build_zip({"1234567890_ACTIVITY.fit": build_fit(1)}), ctx)
    name = manifest["members"][0]["name"]
    assert "1234567890" not in name
    assert name.endswith("_ACTIVITY.fit")
    assert "1234567890" in ctx.originals


def test_short_digit_runs_kept() -> None:
    manifest = analyze_original_file(build_zip({"12345.txt": b"x"}), _ctx())
    assert manifest["members"][0]["name"] == "12345.txt"


def test_unknown_format() -> None:
    manifest = analyze_original_file(b"<html>nope</html>", _ctx())
    assert manifest["format"] == "unknown"
    assert _codes(manifest) == ["unknown_format"]


def test_bad_zip() -> None:
    manifest = analyze_original_file(b"PK\x03\x04garbage-not-a-zip", _ctx())
    assert manifest["format"] == "zip"
    assert _codes(manifest) == ["zip_unreadable"]
