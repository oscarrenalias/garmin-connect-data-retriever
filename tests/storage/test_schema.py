from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.storage.connection import open_write_connection

EXPECTED_TABLES = {
    "archive_metadata",
    "sync_runs",
    "raw_payloads",
    "fetch_events",
    "activity_archive",
    "activity_resources",
    "sync_checkpoints",
    "processing_runs",
    "schema_migrations",
    "activities",
    "activity_sessions",
    "activity_laps",
    "activity_samples",
    "activity_sets",
    "running_metrics",
    "swimming_metrics",
    "cycling_metrics",
    "cardio_metrics",
    "swim_lengths",
    "form_sessions",
    "form_lengths",
}
EXPECTED_INDEXES = {
    "idx_raw_payloads_activity_kind",
    "idx_fetch_events_resource_fetched",
    "idx_fetch_events_sync_run",
    "idx_activity_archive_parent",
    "idx_activity_archive_type_key",
    "idx_activity_resources_state",
    "idx_processing_runs_activity_started",
    "idx_activities_local_date_family",
    "idx_activities_parent",
    "idx_activities_processing_status",
    "idx_activity_samples_timestamp",
}


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "a.db"
    ArchiveClient(ArchiveConfig(db_path=path), clock=FixedClock(), environ={}).initialize()
    c = open_write_connection(path, busy_timeout_ms=1000)
    yield c
    c.close()


def test_exact_tables_and_indexes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {n for t, n in rows if t == "table"} == EXPECTED_TABLES
    assert {n for t, n in rows if t == "index"} == EXPECTED_INDEXES


def test_migration_timestamp_is_exact_fixed_clock_string(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT applied_at_utc FROM schema_migrations WHERE version = 1").fetchone()
    assert row[0] == "2026-01-02T03:04:05.000000Z"


def test_bogus_raw_payload_kind_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO raw_payloads (resource_key, kind, body_blob, sha256, byte_count,"
            " first_seen_at_utc) VALUES ('r', 'bogus', x'00', 'h', 1, 't')"
        )


def test_valid_raw_payload_kind_accepted(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO raw_payloads (resource_key, kind, body_blob, sha256, byte_count,"
        " first_seen_at_utc) VALUES ('r', 'summary', x'00', 'h', 1, 't')"
    )


def test_bogus_resource_state_rejected(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO activity_archive (activity_id, first_seen_at_utc, last_seen_at_utc)"
        " VALUES ('1', 't', 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activity_resources (activity_id, kind, state)"
            " VALUES ('1', 'summary', 'bogus')"
        )


def test_invalid_json_in_sync_runs_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sync_runs (mode, started_at_utc, status, parameters_json)"
            " VALUES ('full', 't', 'running', '{not json')"
        )


def test_activity_resource_requires_existing_activity(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activity_resources (activity_id, kind, state)"
            " VALUES ('missing', 'summary', 'pending')"
        )


def test_form_lengths_columns_match_swim_lengths(conn: sqlite3.Connection) -> None:
    def cols(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    swim = cols("swim_lengths") - {"activity_id", "processing_run_id"}
    form = cols("form_lengths") - {"form_session_id"}
    assert swim == form


def test_form_tables_store_no_serial(conn: sqlite3.Connection) -> None:
    for table in ("form_sessions", "form_lengths"):
        names = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert not any("serial" in n for n in names)
