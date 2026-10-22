from __future__ import annotations

import pytest

from garmin_archive.processing import fitfile
from garmin_archive.processing.fitfile import FitUnavailableError, decode_fit, extract_fit
from helpers.fitbuilder import build_fit, cardio_fit, running_fit
from helpers.zipbuilder import build_zip

FIT = build_fit(2)


def test_extracts_fit_from_valid_zip() -> None:
    assert extract_fit(build_zip({"a.fit": FIT})) == FIT


def test_uppercase_extension_accepted() -> None:
    assert extract_fit(build_zip({"A.FIT": FIT})) == FIT


def test_first_fit_member_wins_and_other_members_skipped() -> None:
    other = build_fit(5)
    body = build_zip({"readme.txt": b"x", "one.fit": FIT, "two.fit": other})
    assert extract_fit(body) == FIT


@pytest.mark.parametrize(
    "name", ["../x.fit", "/abs/x.fit", "..\\x.fit", "C:\\x.fit", "a/../../x.fit", "\\\\srv\\x.fit"]
)
def test_unsafe_member_paths_rejected(name: str) -> None:
    with pytest.raises(FitUnavailableError):
        extract_fit(build_zip({name: FIT}))


def test_unsafe_member_rejected_even_when_not_fit() -> None:
    with pytest.raises(FitUnavailableError):
        extract_fit(build_zip({"ok.fit": FIT, "../evil.txt": b"x"}))


@pytest.mark.parametrize("body", [b"", b"not a zip at all", b"PK\x03\x04garbage"])
def test_non_zip_body_rejected(body: bytes) -> None:
    with pytest.raises(FitUnavailableError):
        extract_fit(body)


def test_too_many_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fitfile, "MAX_ZIP_ENTRIES", 3)
    body = build_zip({f"f{i}.txt": b"x" for i in range(4)} | {"a.fit": FIT})
    with pytest.raises(FitUnavailableError, match="too many"):
        extract_fit(body)


def test_oversized_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fitfile, "MAX_FIT_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(FitUnavailableError, match="size"):
        extract_fit(build_zip({"a.fit": FIT}))


def test_oversized_zip_total(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fitfile, "MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(FitUnavailableError, match="size"):
        extract_fit(build_zip({"a.fit": FIT}))


def test_zip_without_fit_member() -> None:
    with pytest.raises(FitUnavailableError, match="no .fit"):
        extract_fit(build_zip({"a.gpx": b"<gpx/>"}))


def test_decode_running_fit() -> None:
    messages = decode_fit(running_fit())
    assert (len(messages.sessions), len(messages.laps), len(messages.records)) == (1, 3, 100)
    assert messages.sets == []


def test_decode_cardio_fit_has_sets_and_no_laps() -> None:
    messages = decode_fit(cardio_fit())
    assert messages.laps == []
    assert len(messages.sets) == 10
    assert len(messages.sessions) == 1


def test_garbage_bytes_undecodable() -> None:
    with pytest.raises(FitUnavailableError):
        decode_fit(b"garbage bytes that are not fit")


def test_empty_bytes_undecodable() -> None:
    with pytest.raises(FitUnavailableError):
        decode_fit(b"")


def test_truncated_fit() -> None:
    with pytest.raises(FitUnavailableError):
        decode_fit(running_fit()[:-30])


def test_crc_corrupt_fit() -> None:
    data = bytearray(running_fit())
    data[-1] ^= 0xFF
    with pytest.raises(FitUnavailableError):
        decode_fit(bytes(data))
