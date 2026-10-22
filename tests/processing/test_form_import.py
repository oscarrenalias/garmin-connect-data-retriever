from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from garmin_fit_sdk import Encoder

from garmin_archive.archive import ArchiveWriter
from garmin_archive.processing.form_import import import_form_files, import_form_zip_bytes
from helpers.archive_seed import FixedClock
from helpers.zipbuilder import build_zip

SERIAL = 987654321


def _fit(manufacturer: str = "form", serial: int = SERIAL, product: int = 0) -> bytes:
    encoder = Encoder()
    encoder.write_mesg(
        {
            "mesg_num": 0,
            "type": "activity",
            "manufacturer": manufacturer,
            "product": product,
            "serial_number": serial,
        }
    )
    return encoder.close()


@pytest.fixture
def writer(conn: sqlite3.Connection, clock: FixedClock) -> ArchiveWriter:
    return ArchiveWriter(conn, clock)


def _payload_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0])


def test_fit_and_zip_with_two_files_import(
    writer: ArchiveWriter, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    single = tmp_path / "a.fit"
    single.write_bytes(_fit(product=1))
    archive = tmp_path / "b.zip"
    archive.write_bytes(build_zip({"x.fit": _fit(product=2), "y.fit": _fit(product=3)}))
    report = import_form_files(writer, [single, archive])
    assert (report.imported, report.duplicate, report.rejected) == (3, 0, [])
    assert _payload_count(conn) == 3


def test_garmin_fit_rejected_without_serial(
    writer: ArchiveWriter, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "g.fit"
    path.write_bytes(_fit("garmin"))
    report = import_form_files(writer, [path])
    assert report.imported == 0
    assert len(report.rejected) == 1
    assert report.rejected[0].file == "g.fit"
    assert report.rejected[0].reason
    assert str(SERIAL) not in repr(report)
    assert _payload_count(conn) == 0


def test_zip_with_dotdot_member_rejected(writer: ArchiveWriter, conn: sqlite3.Connection) -> None:
    body = build_zip({"../evil.fit": _fit(), "ok.fit": _fit(product=4)})
    report = import_form_zip_bytes(writer, body)
    assert report.imported == 0
    assert len(report.rejected) == 1
    assert _payload_count(conn) == 0


def test_reimport_is_duplicate_with_no_new_rows(
    writer: ArchiveWriter, conn: sqlite3.Connection
) -> None:
    body = build_zip({"x.fit": _fit()})
    first = import_form_zip_bytes(writer, body)
    events = int(conn.execute("SELECT COUNT(*) FROM fetch_events").fetchone()[0])
    second = import_form_zip_bytes(writer, body)
    assert first.imported == 1
    assert (second.imported, second.duplicate) == (0, 1)
    assert _payload_count(conn) == 1
    assert int(conn.execute("SELECT COUNT(*) FROM fetch_events").fetchone()[0]) == events


def test_invalid_inputs_rejected(writer: ArchiveWriter, tmp_path: Path) -> None:
    bad = tmp_path / "bad.fit"
    bad.write_bytes(b"not a fit file")
    other = tmp_path / "notes.txt"
    other.write_text("x")
    report = import_form_files(writer, [bad, other, tmp_path / "missing.fit"])
    assert report.imported == 0
    assert [r.file for r in report.rejected] == ["bad.fit", "notes.txt", "missing.fit"]
    not_zip = import_form_zip_bytes(writer, b"junk")
    assert len(not_zip.rejected) == 1
