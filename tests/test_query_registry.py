from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from garmin_archive.models import InvalidQueryError
from garmin_archive.query import REGISTRY, list_queryable, lookup, quote_identifier
from garmin_archive.storage.migrations import apply_migrations


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", isolation_level=None)
    apply_migrations(c, FixedClock())
    yield c
    c.close()


def test_entries_have_description_and_grain() -> None:
    assert len({e.name for e in REGISTRY}) == len(REGISTRY)
    for e in REGISTRY:
        assert e.description
        assert e.grain


def test_all_registry_objects_exist_on_initialised_db(conn: sqlite3.Connection) -> None:
    live = {o.name: o for o in list_queryable(conn)}
    assert set(live) == {e.name for e in REGISTRY}
    assert live["activities"].kind == "table"
    assert live["v_running"].kind == "view"


def test_absent_objects_omitted() -> None:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE activities (activity_id TEXT PRIMARY KEY, n INTEGER)")
    assert [o.name for o in list_queryable(c)] == ["activities"]
    with pytest.raises(InvalidQueryError):
        lookup(c, "v_running")


def test_columns_from_live_schema() -> None:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE activities (activity_id TEXT PRIMARY KEY, n INTEGER)")
    obj = lookup(c, "activities")
    assert [(col.name, col.type, col.pk) for col in obj.columns] == [
        ("activity_id", "TEXT", True),
        ("n", "INTEGER", False),
    ]


def test_views_reference_only_registry_objects(conn: sqlite3.Connection) -> None:
    names = {e.name for e in REGISTRY}
    all_objects = {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    views = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
    assert views
    for name, sql in views:
        assert name in names
        refs = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)", sql, flags=re.IGNORECASE))
        assert refs
        ctes = set(re.findall(r"\b(?:WITH|,)\s+([A-Za-z_]\w*)\s+AS\s*\(", sql, flags=re.IGNORECASE))
        refs -= ctes  # a CTE name is a local alias, not a schema object
        assert refs
        assert refs <= all_objects
        assert refs <= names, f"{name} reads unregistered {refs - names}"


def test_authorizer_confirms_view_base_tables(conn: sqlite3.Connection) -> None:
    names = {e.name for e in REGISTRY}
    seen: set[str] = set()

    def auth(action: int, a1: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ and a1:
            seen.add(a1)
        return sqlite3.SQLITE_OK

    for e in REGISTRY:
        conn.set_authorizer(auth)
        conn.execute(f"SELECT * FROM {quote_identifier(e.name)} LIMIT 0").fetchall()
        conn.set_authorizer(None)
    assert seen <= names


@pytest.mark.parametrize(
    "name", ["sqlite_master", "schema_migrations", "garbage", "", "fetch_events", "ACTIVITIES"]
)
def test_unknown_names_rejected(conn: sqlite3.Connection, name: str) -> None:
    with pytest.raises(InvalidQueryError):
        lookup(conn, name)


def test_lookup_known(conn: sqlite3.Connection) -> None:
    obj = lookup(conn, "v_swimming")
    assert obj.kind == "view"
    assert obj.columns


def test_quote_identifier() -> None:
    assert quote_identifier("a") == '"a"'
    assert quote_identifier('a"b') == '"a""b"'
    with pytest.raises(InvalidQueryError):
        quote_identifier("a\x00b")


def test_form_tables_registered_and_queryable(conn: sqlite3.Connection) -> None:
    for name in ("form_sessions", "form_lengths"):
        assert lookup(conn, name).kind == "table"
        assert conn.execute(f"SELECT * FROM {quote_identifier(name)} LIMIT 0").fetchall() == []


def test_form_schema_notes_verbatim(conn: sqlite3.Connection) -> None:
    swim = (
        "Pool swims prefer FORM goggle data when available (`data_source`); Garmin originals "
        "are in `garmin_*`. HR, calories and training metrics are always Garmin."
    )
    summary = (
        "Garmin values for Garmin activities, even when FORM data exists, so swim totals here "
        "can differ from v_swimming; rows with `data_source = 'form'` are goggle-only swims."
    )
    assert swim in lookup(conn, "v_swimming").notes
    assert swim in lookup(conn, "v_swim_lengths").notes
    assert summary in lookup(conn, "v_activity_summary").notes
    assert (
        lookup(conn, "form_sessions").description
        == "FORM-recorded sessions and their match to a Garmin swim."
    )
