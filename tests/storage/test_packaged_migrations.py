from __future__ import annotations

import sqlite3

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


def test_packaged_migrations_load_versions_one_to_three() -> None:
    migrations = PackagedMigrations().load()

    assert [m.version for m in migrations] == [1, 2, 3]
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
