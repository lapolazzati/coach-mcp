"""Readiness scoring (U6): flag last night's recovery against a 28-day baseline.

Pure scoring over plain row mappings, plus one writer that stores the result
through coach.store.derived. Signals:

- hrv: overnight HRV below baseline mean - 1 SD
- rhr: resting HR above baseline mean + 5 bpm
- sleep: total sleep under 5.5 h
- body_battery: body_battery_high under 30 (proxy for body battery at wake)

HRV and RHR need >= 14 valid days in the prior 28; without either baseline the
day is "unknown". A missing signal is listed and never counts toward a flag.
0-1 flags is green, 2 amber, 3+ red.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from coach.store import derived
from coach.store.db import fetch_all, fetch_one, parse_date

BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 14
RHR_MARGIN_BPM = 5.0
SLEEP_MIN_SECS = int(5.5 * 3600)
BODY_BATTERY_MIN = 30
SIGNALS = ("hrv", "rhr", "sleep", "body_battery")
COLUMNS = {"hrv": "hrv_ms", "rhr": "resting_hr", "sleep": "sleep_secs", "body_battery": "body_battery_high"}
FLAG_NAMES = {"hrv": "hrv_low", "rhr": "rhr_high", "sleep": "sleep_short", "body_battery": "body_battery_low"}

Wellness = Mapping[str, Any]


@dataclass(frozen=True)
class Readiness:
    day: str
    status: str
    flags: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


def _value(row: Wellness | None, signal: str) -> float | None:
    if row is None:
        return None
    raw = row.get(COLUMNS[signal])
    if raw is None:
        return None
    value = float(raw)
    return value if value > 0 else None


def baseline(history: Sequence[Wellness], signal: str) -> tuple[float, float] | None:
    """Mean and SD of a signal over the history rows, or None if fewer than 14 valid days."""
    values = [v for v in (_value(r, signal) for r in history) if v is not None]
    if len(values) < MIN_BASELINE_DAYS:
        return None
    return statistics.fmean(values), statistics.stdev(values)


def score(day: str | date, today: Wellness | None, history: Sequence[Wellness]) -> Readiness:
    """Score one day. `history` is the prior days' rows (only the last 28 by date are expected)."""
    key = parse_date(day).isoformat()
    hrv_base = baseline(history, "hrv")
    rhr_base = baseline(history, "rhr")
    values = {s: _value(today, s) for s in SIGNALS}
    signals: dict[str, Any] = {s: values[s] for s in SIGNALS}
    if hrv_base is not None:
        signals["hrv_baseline"] = {"mean": round(hrv_base[0], 1), "sd": round(hrv_base[1], 1)}
    if rhr_base is not None:
        signals["rhr_baseline"] = {"mean": round(rhr_base[0], 1)}

    missing = [s for s in SIGNALS if values[s] is None]
    if hrv_base is None and rhr_base is None:
        return Readiness(key, "unknown", [], missing, {**signals, "reason": "insufficient_baseline"})
    if len(missing) == len(SIGNALS):
        return Readiness(key, "unknown", [], missing, {**signals, "reason": "no_signals"})

    flags: list[str] = []
    hrv, rhr, sleep, bb = values["hrv"], values["rhr"], values["sleep"], values["body_battery"]
    if hrv is None or hrv_base is None:
        if "hrv" not in missing:
            missing.append("hrv")
    elif hrv < hrv_base[0] - hrv_base[1]:
        flags.append(FLAG_NAMES["hrv"])
    if rhr is None or rhr_base is None:
        if "rhr" not in missing:
            missing.append("rhr")
    elif rhr > rhr_base[0] + RHR_MARGIN_BPM:
        flags.append(FLAG_NAMES["rhr"])
    if sleep is not None and sleep < SLEEP_MIN_SECS:
        flags.append(FLAG_NAMES["sleep"])
    if bb is not None and bb < BODY_BATTERY_MIN:
        flags.append(FLAG_NAMES["body_battery"])

    missing = [s for s in SIGNALS if s in missing]
    status = "red" if len(flags) >= 3 else "amber" if len(flags) == 2 else "green"
    return Readiness(key, status, flags, missing, signals)


def load(conn: sqlite3.Connection, day: str | date) -> tuple[Wellness | None, list[Wellness]]:
    """Today's wellness row and the prior 28 days."""
    d = parse_date(day)
    start = (d - timedelta(days=BASELINE_DAYS)).isoformat()
    today = fetch_one(conn, "SELECT * FROM wellness_day WHERE date = ?", (d.isoformat(),))
    history = fetch_all(
        conn, "SELECT * FROM wellness_day WHERE date >= ? AND date < ? ORDER BY date", (start, d.isoformat())
    )
    return today, list(history)


def compute_and_store(conn: sqlite3.Connection, day: str | date, *, final: bool = False) -> tuple[Readiness, bool]:
    """Score `day` from the DB and store it. Returns the score and whether the row was written."""
    today, history = load(conn, day)
    result = score(day, today, history)
    stored = derived.upsert_readiness(
        conn,
        result.day,
        status=result.status,
        flags=result.flags,
        signals={**result.signals, "missing": result.missing},
        final=final,
    )
    return result, stored
