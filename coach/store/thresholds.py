"""Manual athlete thresholds (FTP, weight) that take precedence over device values.

A value applies from its effective_from date until a later row for the same kind.
Writes are audited and undoable like the other store writes.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from coach.store import audit
from coach.store.db import Row, fetch_all, fetch_one, now_utc, parse_date, transaction
from coach.store.logs import LogResult

LIMITS = {"ftp_w": (80.0, 500.0), "weight_kg": (40.0, 150.0)}


def set_threshold(
    conn: sqlite3.Connection,
    *,
    kind: str,
    value: float,
    effective_from: str | date,
    note: str | None = None,
    actor: str = "claude",
) -> LogResult:
    """Insert or replace the value for (kind, effective_from); the row id stays stable on replace."""
    if kind not in LIMITS:
        raise ValueError(f"kind must be one of {tuple(LIMITS)}, got {kind!r}")
    low, high = LIMITS[kind]
    if not low <= value <= high:
        raise ValueError(f"{kind} must be between {low:g} and {high:g}, got {value:g}")
    day = parse_date(effective_from).isoformat()
    ts = now_utc()
    with transaction(conn):
        previous = fetch_one(conn, "SELECT * FROM athlete_threshold WHERE kind = ? AND effective_from = ?",
                             (kind, day))
        conn.execute(
            "INSERT INTO athlete_threshold (kind, value, effective_from, note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (kind, effective_from)"
            " DO UPDATE SET value = excluded.value, note = excluded.note, updated_at = excluded.updated_at",
            (kind, float(value), day, note, ts, ts),
        )
        row = fetch_one(conn, "SELECT * FROM athlete_threshold WHERE kind = ? AND effective_from = ?", (kind, day))
        assert row is not None
        aid = audit.record(conn, actor=actor, action="set_athlete_settings", entity="athlete_threshold",
                           entity_id=row["id"], before=previous, after=row, reason=note)
    return LogResult("replaced" if previous else "created", row, aid, previous=previous)


def effective(conn: sqlite3.Connection, day: str | date) -> dict[str, Row]:
    """kind -> the latest row with effective_from <= day; {} if none or the table is missing."""
    try:
        rows = fetch_all(
            conn,
            "SELECT * FROM athlete_threshold t WHERE effective_from = (SELECT max(effective_from)"
            " FROM athlete_threshold WHERE kind = t.kind AND effective_from <= ?)",
            (parse_date(day).isoformat(),),
        )
    except sqlite3.OperationalError:
        return {}
    return {r["kind"]: r for r in rows}
