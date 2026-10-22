from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from garmin_archive.models import AccountMismatchError
from garmin_archive.storage.connection import transaction
from garmin_archive.storage.metadata import (
    bind_account,
    get_account_binding,
    get_metadata,
    set_metadata,
)
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


def test_metadata_roundtrip_and_upsert(conn: sqlite3.Connection) -> None:
    assert get_metadata(conn, "k") is None
    set_metadata(conn, "k", {"a": [1, 2]})
    assert get_metadata(conn, "k") == {"a": [1, 2]}
    set_metadata(conn, "k", "v2")
    assert get_metadata(conn, "k") == "v2"


def test_set_metadata_uses_callers_transaction(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(conn):
        set_metadata(conn, "k", 1)
        raise RuntimeError
    assert get_metadata(conn, "k") is None


def test_first_bind_stores_text_and_timestamp(conn: sqlite3.Connection) -> None:
    assert get_account_binding(conn) is None
    assert bind_account(conn, "12345", FixedClock()) is True
    assert get_account_binding(conn) == "12345"
    assert get_metadata(conn, "account_binding") == {
        "user_profile_id": "12345",
        "bound_at_utc": "2026-01-02T03:04:05.000000Z",
    }
    stored = conn.execute(
        "SELECT json_type(value_json, '$.user_profile_id') FROM archive_metadata "
        "WHERE key='account_binding'"
    ).fetchone()
    assert stored[0] == "text"


def test_same_id_returns_false_without_write(conn: sqlite3.Connection) -> None:
    bind_account(conn, "12345", FixedClock())
    before = get_metadata(conn, "account_binding")

    class Later:
        def now(self) -> datetime:
            return datetime(2030, 1, 1, tzinfo=UTC)

    assert bind_account(conn, "12345", Later()) is False
    assert get_metadata(conn, "account_binding") == before


def test_different_id_raises_without_leaking_ids(conn: sqlite3.Connection) -> None:
    bind_account(conn, "11111", FixedClock())
    before = get_metadata(conn, "account_binding")
    with pytest.raises(AccountMismatchError) as info:
        bind_account(conn, "22222", FixedClock())
    exc = info.value
    for text in (str(exc), exc.message, repr(exc.details)):
        assert "11111" not in text
        assert "22222" not in text
    assert get_account_binding(conn) == "11111"
    assert get_metadata(conn, "account_binding") == before
    assert not conn.in_transaction


def test_empty_id_raises_value_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        bind_account(conn, "", FixedClock())
    assert get_account_binding(conn) is None
