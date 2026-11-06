"""Sync bookkeeping for planned_session <-> intervals.icu (KTD10).

- Every user-visible write calls mark_dirty(): sync_dirty=1 and row_version+1.
- After a push, mark_pushed() clears sync_dirty only when row_version still
  equals the pushed version, so an edit made during the push stays dirty.
- A session that leaves an external_id (a move) leaves a tombstone with
  pending_delete=1 until intervals.icu confirms the delete.
- missing_polls counts consecutive reconciles where a clean row is absent
  from intervals.icu; a push resets it.

None of these functions writes an audit row: they are bookkeeping, not user
changes. The helpers ops call mid-transaction (mark_dirty, add_tombstone,
claim_external_id, reassign_external_ids) join the caller's transaction; the
ones the sync job calls on its own (mark_pushed, record_missing_poll,
reset_missing_polls, confirm_tombstone) open one, nesting when already inside.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from coach.store.db import Row, fetch_all, now_utc, transaction

SLOTS: dict[str, str] = {"am": "07:00", "pm": "18:00"}

# Columns that describe the workout itself (what gets pushed to intervals.icu).
CONTENT_FIELDS = ("external_id", "date", "slot", "start_time", "start_local", "duration_min", "sport", "kind",
                  "name", "session_json", "status")


def external_id(day: str, slot: str) -> str:
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot!r}; expected one of {sorted(SLOTS)}")
    return f"coach:{day}:{slot}"


def content_hash(row: Row) -> str:
    payload = {k: row.get(k) for k in CONTENT_FIELDS}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def mark_dirty(conn: sqlite3.Connection, session_id: int) -> int:
    """Flag a session for push and bump row_version; return the new version."""
    conn.execute(
        "UPDATE planned_session SET sync_dirty = 1, row_version = row_version + 1, updated_at = ? WHERE id = ?",
        (now_utc(), session_id),
    )
    row = conn.execute("SELECT row_version FROM planned_session WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise LookupError(f"planned_session {session_id} not found")
    return int(row[0])


def mark_pushed(
    conn: sqlite3.Connection,
    session_id: int,
    pushed_version: int,
    written_hash: str,
    icu_event_id: int | None = None,
) -> bool:
    """Record a successful push. Clears sync_dirty only if nothing changed since; returns whether it did."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE planned_session SET sync_dirty = 0, last_written_hash = ?, missing_polls = 0,"
            " icu_event_id = COALESCE(?, icu_event_id) WHERE id = ? AND row_version = ?",
            (written_hash, icu_event_id, session_id, pushed_version),
        )
    return cur.rowcount == 1


def dirty_sessions(conn: sqlite3.Connection) -> list[Row]:
    return fetch_all(conn, "SELECT * FROM planned_session WHERE sync_dirty = 1 ORDER BY date, slot")


def record_missing_poll(conn: sqlite3.Connection, session_id: int) -> int:
    """Count one reconcile where a clean row was absent remotely. Dirty or never-pushed rows don't count."""
    with transaction(conn):
        conn.execute(
            "UPDATE planned_session SET missing_polls = missing_polls + 1"
            " WHERE id = ? AND sync_dirty = 0 AND last_written_hash IS NOT NULL",
            (session_id,),
        )
    row = conn.execute("SELECT missing_polls FROM planned_session WHERE id = ?", (session_id,)).fetchone()
    return int(row[0]) if row else 0


def reset_missing_polls(conn: sqlite3.Connection, session_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE planned_session SET missing_polls = 0 WHERE id = ?", (session_id,))


def add_tombstone(conn: sqlite3.Connection, ext_id: str, session_id: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO session_tombstone (external_id, session_id, reason, pending_delete, created_at)"
        " VALUES (?, ?, ?, 1, ?) ON CONFLICT (external_id, session_id) DO UPDATE SET"
        " reason = excluded.reason, pending_delete = 1, created_at = excluded.created_at",
        (ext_id, session_id, reason, now_utc()),
    )


def claim_external_id(conn: sqlite3.Connection, ext_id: str) -> None:
    """A session is taking ext_id again: drop pending deletes so sync doesn't delete the new event."""
    conn.execute("DELETE FROM session_tombstone WHERE external_id = ? AND pending_delete = 1", (ext_id,))


def pending_tombstones(conn: sqlite3.Connection) -> list[Row]:
    return fetch_all(conn, "SELECT * FROM session_tombstone WHERE pending_delete = 1 ORDER BY created_at")


def confirm_tombstone(conn: sqlite3.Connection, ext_id: str, session_id: int) -> None:
    """intervals.icu confirmed the delete of ext_id."""
    with transaction(conn):
        conn.execute(
            "UPDATE session_tombstone SET pending_delete = 0 WHERE external_id = ? AND session_id = ?",
            (ext_id, session_id),
        )


def reassign_external_ids(conn: sqlite3.Connection, changes: Sequence[tuple[int, str, str]], reason: str) -> None:
    """Move sessions to new external_ids in one step: (session_id, old_id, new_id).

    Handles exchanges (a swap) without tripping the UNIQUE constraint: rows get
    a temporary id first, every old id is tombstoned, then every new id is
    claimed. An id that one row leaves and another takes ends with no pending
    tombstone. The remote event id is cleared because it belonged to the old id.
    """
    moving = [c for c in changes if c[1] != c[2]]
    for sid, old, _ in moving:
        conn.execute("UPDATE planned_session SET external_id = ? WHERE id = ?", (f"tmp:{sid}:{old}", sid))
    for sid, old, _ in moving:
        add_tombstone(conn, old, sid, reason)
    for sid, _, new in moving:
        claim_external_id(conn, new)
        conn.execute(
            "UPDATE planned_session SET external_id = ?, icu_event_id = NULL, last_written_hash = NULL WHERE id = ?",
            (new, sid),
        )


def taken_slots(conn: sqlite3.Connection, day: str, exclude_ids: Sequence[int] = ()) -> set[str]:
    rows = conn.execute("SELECT id, slot FROM planned_session WHERE date = ?", (day,)).fetchall()
    return {slot for sid, slot in rows if sid not in exclude_ids}


def as_params(values: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """'a = ?, b = ?' plus params, for UPDATE ... SET with trusted column names."""
    return ", ".join(f"{k} = ?" for k in values), tuple(values.values())
