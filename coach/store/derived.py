"""Derived rows: readiness_day and weekly_summary.

These are recomputed by jobs, not edited by Lapo, so they are not audited.
U6 (readiness scoring) and U10 (weekly summary) extend this module.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from coach.store.db import Row, fetch_one, now_utc, parse_date, transaction

READINESS_STATUSES = ("green", "amber", "red", "unknown")


def get_readiness(conn: sqlite3.Connection, day: str | date) -> Row | None:
    return fetch_one(conn, "SELECT * FROM readiness_day WHERE date = ?", (parse_date(day).isoformat(),))


def mark_sleep_seen(conn: sqlite3.Connection, day: str | date, ts: str | None = None) -> bool:
    """Record when last night's sleep first arrived. Sets sleep_seen_at only if null; returns whether it did."""
    key = parse_date(day).isoformat()
    seen = ts or now_utc()
    with transaction(conn):
        before = get_readiness(conn, key)
        if before is not None and before["sleep_seen_at"] is not None:
            return False
        conn.execute(
            "INSERT INTO readiness_day (date, status, computed_at, sleep_seen_at) VALUES (?, 'unknown', ?, ?)"
            " ON CONFLICT (date) DO UPDATE SET sleep_seen_at = COALESCE(sleep_seen_at, excluded.sleep_seen_at)",
            (key, seen, seen),
        )
    return True


def upsert_readiness(
    conn: sqlite3.Connection,
    day: str | date,
    *,
    status: str,
    flags: list[str],
    signals: dict[str, Any],
    final: bool = False,
    computed_at: str | None = None,
) -> bool:
    """Write the day's score, keeping sleep_seen_at. A final row is not overwritten by a non-final one."""
    if status not in READINESS_STATUSES:
        raise ValueError(f"status must be one of {READINESS_STATUSES}")
    key = parse_date(day).isoformat()
    with transaction(conn):
        current = get_readiness(conn, key)
        if current is not None and current["final"] and not final:
            return False
        conn.execute(
            "INSERT INTO readiness_day (date, status, flags_json, signals_json, computed_at, final)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (date) DO UPDATE SET status = excluded.status,"
            " flags_json = excluded.flags_json, signals_json = excluded.signals_json,"
            " computed_at = excluded.computed_at, final = excluded.final",
            (key, status, json.dumps(flags), json.dumps(signals, sort_keys=True, default=str),
             computed_at or now_utc(), int(final)),
        )
    return True


def upsert_weekly_summary(
    conn: sqlite3.Connection,
    week_start: str | date,
    metrics: dict[str, Any],
    computed_at: str | None = None,
) -> None:
    monday = parse_date(week_start)
    if monday.weekday() != 0:
        raise ValueError(f"week_start must be a Monday, got {monday.isoformat()}")
    with transaction(conn):
        conn.execute(
            "INSERT INTO weekly_summary (week_start, metrics_json, computed_at) VALUES (?, ?, ?)"
            " ON CONFLICT (week_start) DO UPDATE SET metrics_json = excluded.metrics_json,"
            " computed_at = excluded.computed_at",
            (monday.isoformat(), json.dumps(metrics, sort_keys=True, default=str), computed_at or now_utc()),
        )
