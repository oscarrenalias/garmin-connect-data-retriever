"""``rebuild_form``: wholesale rebuild of form_sessions / form_lengths with matching."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_fit_sdk import Encoder

from garmin_archive.archive import ArchiveWriter
from garmin_archive.processing.form import rebuild_form
from helpers.archive_seed import FixedClock

START = datetime(2026, 6, 25, 6, 42, 28, tzinfo=UTC)
START_Z = "2026-06-25T06:42:28Z"


def _form_fit(offset: float = 0, strokes: int = 15) -> bytes:
    start = START + timedelta(seconds=offset)
    encoder = Encoder()
    encoder.write_mesg({"mesg_num": 0, "type": "activity", "manufacturer": "form", "product": 0})
    for i in range(2):
        encoder.write_mesg(
            {
                "mesg_num": 101,
                "start_time": start + timedelta(seconds=30 * i),
                "length_type": "active",
                "total_timer_time": 20.0,
                "total_elapsed_time": 21.0,
                "swim_stroke": "freestyle",
                "total_strokes": strokes,
            }
        )
    encoder.write_mesg(
        {
            "mesg_num": 18,
            "start_time": start,
            "sport": "swimming",
            "sub_sport": "lap_swimming",
            "pool_length": 25.0,
            "pool_length_unit": "metric",
            "total_distance": 50.0,
            "total_timer_time": 60.0,
            "total_elapsed_time": 60.0,
        }
    )
    return encoder.close()


def _import(conn: sqlite3.Connection, clock: FixedClock, body: bytes) -> None:
    ArchiveWriter(conn, clock).record_import(body=body, filename="x.fit")


def _seed_swim(conn: sqlite3.Connection, clock: FixedClock, activity_id: str, start: str) -> None:
    ArchiveWriter(conn, clock).upsert_activity(
        activity_id=activity_id, owner_profile_id=None, parent_activity_id=None, type_key=None
    )
    run_id = conn.execute(
        "INSERT INTO processing_runs (activity_id, processor_version, input_fingerprint, status,"
        " started_at_utc) VALUES (?, 'v', 'f', 'processed', ?)",
        (activity_id, start),
    ).lastrowid
    conn.execute(
        "INSERT INTO activities (activity_id, processing_run_id, sport_family, start_at_utc,"
        " elapsed_duration_s, processing_status) VALUES (?, ?, 'swimming', ?, 60, 'processed')",
        (activity_id, run_id, start),
    )
    conn.execute(
        "INSERT INTO swimming_metrics (activity_id, processing_run_id, swim_environment,"
        " pool_length_m) VALUES (?, ?, 'pool', 25.0)",
        (activity_id, run_id),
    )


def _dump(conn: sqlite3.Connection) -> list[list[tuple[Any, ...]]]:
    return [
        conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()  # noqa: S608
        for t in ("form_sessions", "form_lengths")
    ]


def test_unmatched_rebuild_writes_rows_and_no_processing_runs(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    _import(conn, clock, _form_fit())
    result = rebuild_form(conn)
    assert (result.matched, result.unmatched) == (0, 1)
    row = conn.execute(
        "SELECT start_at_utc, matched_activity_id, source_payload_id FROM form_sessions"
    ).fetchone()
    assert row[0] == START_Z
    assert row[1] is None
    assert row[2] == conn.execute("SELECT id FROM raw_payloads").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM form_lengths").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0] == 0


def test_rebuild_twice_is_identical(conn: sqlite3.Connection, clock: FixedClock) -> None:
    _import(conn, clock, _form_fit())
    _import(conn, clock, _form_fit(offset=7200))
    _seed_swim(conn, clock, "100", START_Z)
    first = rebuild_form(conn)
    before = _dump(conn)
    second = rebuild_form(conn)
    assert first == second
    assert _dump(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM form_sessions").fetchone()[0] == 2


def test_form_imported_before_garmin_matches_after_rebuild(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    _import(conn, clock, _form_fit(offset=8))
    assert rebuild_form(conn).matched == 0
    _seed_swim(conn, clock, "100", START_Z)
    result = rebuild_form(conn)
    assert (result.matched, result.unmatched) == (1, 0)
    row = conn.execute(
        "SELECT matched_activity_id, match_delta_s, pool_length_mismatch FROM form_sessions"
    ).fetchone()
    assert tuple(row) == ("100", 8.0, 0)


def test_garmin_swim_removed_unmatches_on_rebuild(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    _import(conn, clock, _form_fit())
    _seed_swim(conn, clock, "100", START_Z)
    assert rebuild_form(conn).matched == 1
    conn.execute("DELETE FROM swimming_metrics")
    assert rebuild_form(conn).matched == 0
    assert conn.execute("SELECT matched_activity_id FROM form_sessions").fetchone()[0] is None


def test_undecodable_payload_skipped(conn: sqlite3.Connection, clock: FixedClock) -> None:
    _import(conn, clock, _form_fit())
    ArchiveWriter(conn, clock).record_import(body=b"not a fit file at all", filename="bad.fit")
    result = rebuild_form(conn)
    assert (result.unmatched, result.skipped) == (1, 1)


def test_form_fit_without_sessions_is_empty_not_an_error(
    conn: sqlite3.Connection, clock: FixedClock
) -> None:
    encoder = Encoder()
    encoder.write_mesg({"mesg_num": 0, "type": "activity", "manufacturer": "form", "product": 0})
    encoder.write_mesg(
        {
            "mesg_num": 101,
            "start_time": START,
            "length_type": "active",
            "total_timer_time": 20.0,
            "total_elapsed_time": 21.0,
        }
    )
    _import(conn, clock, encoder.close())
    _import(conn, clock, _form_fit())
    result = rebuild_form(conn)
    assert (result.matched, result.unmatched) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM form_sessions").fetchone()[0] == 1
