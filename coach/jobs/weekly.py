"""Sunday 20:00 weekly summary for the Monday-start week, with drift flags."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from coach.jobs.morning import local_now
from coach.store import derived, logs
from coach.store.db import fetch_all, fetch_one

SLEEP_MIN_HOURS = 6.0
RED_DAYS_MAX = 3


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _ctl(conn: sqlite3.Connection, day: date) -> float | None:
    row = fetch_one(conn, "SELECT ctl FROM wellness_day WHERE date <= ? AND ctl IS NOT NULL ORDER BY date DESC LIMIT 1",
                    (day.isoformat(),))
    return None if row is None else float(row["ctl"])


def _strictly(values: list[float | None], rising: bool) -> bool:
    """True when every value is known and each one moves the same way as the one before."""
    known = [v for v in values if v is not None]
    if len(known) != len(values):
        return False
    return all((b > a) if rising else (b < a) for a, b in pairwise(known))


def metrics(conn: sqlite3.Connection, monday: date) -> dict[str, Any]:
    sunday = monday + timedelta(days=6)
    after = sunday + timedelta(days=1)
    span = (monday.isoformat(), sunday.isoformat())

    weights = [logs.weight_mean_7d(conn, (after - timedelta(days=7 * k)).isoformat()) for k in (2, 1, 0)]
    ctls = [_ctl(conn, sunday - timedelta(days=7 * k)) for k in (3, 2, 1, 0)]
    statuses = [r["status"] for r in fetch_all(
        conn, "SELECT status FROM readiness_day WHERE date BETWEEN ? AND ?", span)]
    sleeps = [float(r["sleep_secs"]) for r in fetch_all(
        conn, "SELECT sleep_secs FROM wellness_day WHERE date BETWEEN ? AND ? AND sleep_secs > 0", span)]
    sleep_avg_h = round(statistics.fmean(sleeps) / 3600, 2) if sleeps else None
    red_days = statuses.count("red")

    flags = []
    if _strictly(weights, rising=True):
        flags.append("weight_rising_2w")
    if _strictly(ctls, rising=False):
        flags.append("ctl_falling_3w")
    if red_days >= RED_DAYS_MAX:
        flags.append("red_days")
    if sleep_avg_h is not None and sleep_avg_h < SLEEP_MIN_HOURS:
        flags.append("sleep_short")
    return {
        "week_start": monday.isoformat(),
        "weight_7d": weights[-1],
        "weight_7d_prev": weights[:-1],
        "ctl": ctls[-1],
        "ctl_prev": ctls[:-1],
        "readiness": {s: statuses.count(s) for s in ("green", "amber", "red", "unknown")},
        "sleep_avg_h": sleep_avg_h,
        "flags": flags,
    }


def run(conn: sqlite3.Connection, now: datetime, *, dry_run: bool = False) -> dict[str, Any]:
    monday = week_start(local_now(now).date())
    result = metrics(conn, monday)
    if not dry_run:
        derived.upsert_weekly_summary(conn, monday, result)
    return result
