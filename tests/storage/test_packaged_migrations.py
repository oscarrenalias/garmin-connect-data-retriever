from __future__ import annotations

import sqlite3

import pytest

from garmin_archive.storage.migrations import PackagedMigrations

EXPECTED_TABLES = {
    "archive_metadata",
    "sync_runs",
    "raw_payloads",
    "fetch_events",
    "activity_archive",
    "activity_resources",
    "sync_checkpoints",
    "processing_runs",
}


def test_packaged_migrations_load_versions_one_to_four() -> None:
    migrations = PackagedMigrations().load()

    assert [m.version for m in migrations] == [1, 2, 3, 4, 5, 6]
    assert migrations[0].name == "archive"
    assert migrations[1].name == "activities"
    assert migrations[2].name == "sport_metrics"


def test_archive_script_creates_expected_schema() -> None:
    sql = PackagedMigrations().load()[0].sql
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(sql)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    indexes = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    assert tables == EXPECTED_TABLES
    assert len(indexes) == 7
    assert conn.execute("PRAGMA foreign_key_list(activity_archive)").fetchall() == []


SPORT_TABLES = {
    "running_metrics",
    "swimming_metrics",
    "cycling_metrics",
    "cardio_metrics",
    "swim_lengths",
}


def _full_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    for migration in PackagedMigrations().load():
        conn.executescript(migration.sql)
    return conn


def test_sport_metrics_migration_creates_five_tables() -> None:
    conn = _full_schema()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= SPORT_TABLES


def _seed_parent(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO activity_archive (activity_id, first_seen_at_utc, last_seen_at_utc)"
        " VALUES ('a1', 't', 't')"
    )
    conn.execute(
        "INSERT INTO processing_runs (id, activity_id, processor_version, input_fingerprint,"
        " status, started_at_utc) VALUES (1, 'a1', 'v', 'f', 'processed', 't')"
    )
    conn.execute(
        "INSERT INTO activities (activity_id, processing_run_id, processing_status)"
        " VALUES ('a1', 1, 'processed')"
    )


@pytest.mark.parametrize(
    ("column", "bad", "good"),
    [
        ("swim_environment", "lake", "pool"),
        ("length_coverage_status", "bogus", "complete"),
    ],
)
def test_swimming_check_constraints(column: str, bad: str, good: str) -> None:
    conn = _full_schema()
    _seed_parent(conn)
    sql = (
        f"INSERT INTO swimming_metrics (activity_id, processing_run_id, {column})"
        " VALUES ('a1', 1, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (bad,))
    conn.execute(sql, (good,))


@pytest.mark.parametrize("table", sorted(SPORT_TABLES))
def test_sport_tables_require_existing_activity(table: str) -> None:
    conn = _full_schema()
    _seed_parent(conn)
    extra = ", length_index" if table == "swim_lengths" else ""
    values = ", 0" if table == "swim_lengths" else ""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {table} (activity_id, processing_run_id{extra})"
            f" VALUES ('nope', 1{values})"
        )
    conn.execute(
        f"INSERT INTO {table} (activity_id, processing_run_id{extra}) VALUES ('a1', 1{values})"
    )


def test_swim_lengths_primary_key_is_activity_and_length_index() -> None:
    conn = _full_schema()
    _seed_parent(conn)
    sql = (
        "INSERT INTO swim_lengths (activity_id, length_index, processing_run_id)"
        " VALUES ('a1', ?, 1)"
    )
    conn.execute(sql, (0,))
    conn.execute(sql, (1,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (0,))


def test_metrics_tables_allow_one_row_per_activity() -> None:
    conn = _full_schema()
    _seed_parent(conn)
    sql = "INSERT INTO running_metrics (activity_id, processing_run_id) VALUES ('a1', 1)"
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
