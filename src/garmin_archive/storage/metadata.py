"""Archive metadata key/value access and account binding."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from garmin_archive.models import AccountMismatchError, Clock, utc_rfc3339
from garmin_archive.storage.connection import transaction

ACCOUNT_BINDING_KEY = "account_binding"


def get_metadata(conn: sqlite3.Connection, key: str) -> object | None:
    """Return the JSON-decoded value stored under ``key``, or ``None`` if absent."""
    row = conn.execute("SELECT value_json FROM archive_metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set_metadata(conn: sqlite3.Connection, key: str, value: object) -> None:
    """Upsert a JSON-encoded value; the caller owns the transaction boundary."""
    conn.execute(
        "INSERT INTO archive_metadata(key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )


def get_account_binding(conn: sqlite3.Connection) -> str | None:
    """Return the bound ``user_profile_id``, or ``None`` when unbound."""
    value = get_metadata(conn, ACCOUNT_BINDING_KEY)
    if not isinstance(value, dict):
        return None
    profile_id = cast("dict[str, object]", value).get("user_profile_id")
    return profile_id if isinstance(profile_id, str) else None


def bind_account(conn: sqlite3.Connection, user_profile_id: str, clock: Clock) -> bool:
    """Bind the archive to a Garmin account.

    Returns ``True`` when a new binding was stored and ``False`` when the same
    account was already bound. A different account raises
    ``AccountMismatchError`` without disclosing either ID.
    """
    if not isinstance(user_profile_id, str) or not user_profile_id:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("user_profile_id must be a non-empty string")
    with transaction(conn):
        existing = get_account_binding(conn)
        if existing is None:
            set_metadata(
                conn,
                ACCOUNT_BINDING_KEY,
                {
                    "user_profile_id": user_profile_id,
                    "bound_at_utc": utc_rfc3339(clock.now()),
                },
            )
            return True
        if existing == user_profile_id:
            return False
        raise AccountMismatchError("Archive is bound to a different Garmin account")
