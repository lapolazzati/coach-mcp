"""Readiness scoring (U6)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from store_helpers import fresh_db

from coach import readiness
from coach.store import derived

DAY = "2026-11-06"


def _history(n: int = 28, end: str = DAY, **over: Any) -> list[dict[str, Any]]:
    d = date.fromisoformat(end)
    rows = []
    for i in range(n, 0, -1):
        rows.append({"date": (d - timedelta(days=i)).isoformat(), "hrv_ms": 60 + (i % 5) - 2,
                     "resting_hr": 48 + (i % 3) - 1, "sleep_secs": 7 * 3600, "body_battery_high": 80, **over})
    return rows


NORMAL = {"date": DAY, "hrv_ms": 60, "resting_hr": 48, "sleep_secs": 7 * 3600, "body_battery_high": 80}


def test_all_normal_is_green():
    r = readiness.score(DAY, NORMAL, _history())
    assert r.status == "green" and r.flags == [] and r.missing == []


def test_short_sleep_and_low_hrv_is_amber():
    r = readiness.score(DAY, {**NORMAL, "sleep_secs": 5 * 3600, "hrv_ms": 50}, _history())
    assert r.status == "amber" and set(r.flags) == {"hrv_low", "sleep_short"}


def test_one_flag_is_still_green():
    r = readiness.score(DAY, {**NORMAL, "sleep_secs": 5 * 3600}, _history())
    assert r.status == "green" and r.flags == ["sleep_short"]


def test_three_flags_is_red():
    today = {**NORMAL, "sleep_secs": 4 * 3600, "hrv_ms": 45, "resting_hr": 56}
    r = readiness.score(DAY, today, _history())
    assert r.status == "red" and len(r.flags) == 3


def test_ten_days_history_is_unknown():
    r = readiness.score(DAY, NORMAL, _history(10))
    assert r.status == "unknown" and r.signals["reason"] == "insufficient_baseline"


def test_no_hrv_scores_other_three_and_lists_missing():
    today = {**NORMAL, "hrv_ms": None, "sleep_secs": 5 * 3600, "resting_hr": 56}
    r = readiness.score(DAY, today, _history())
    assert r.missing == ["hrv"] and r.status == "amber" and "hrv_low" not in r.flags


def test_missing_signals_never_make_red():
    today = {"date": DAY, "hrv_ms": None, "resting_hr": None, "sleep_secs": 4 * 3600, "body_battery_high": 20}
    r = readiness.score(DAY, today, _history())
    assert r.status == "amber" and r.missing == ["hrv", "rhr"]


def test_no_signals_is_unknown():
    r = readiness.score(DAY, None, _history())
    assert r.status == "unknown" and r.missing == list(readiness.SIGNALS)


def test_zero_values_are_treated_as_missing():
    r = readiness.score(DAY, {**NORMAL, "hrv_ms": 0}, _history())
    assert "hrv" in r.missing


def test_hrv_baseline_alone_is_enough():
    r = readiness.score(DAY, NORMAL, _history(resting_hr=None))
    assert r.status == "green" and r.missing == ["rhr"]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


def _insert(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        r = {**r, "updated_at": "2026-11-06T00:00:00+00:00"}
        cols = ", ".join(r)
        marks = ", ".join("?" * len(r))
        conn.execute(f"INSERT INTO wellness_day ({cols}) VALUES ({marks})", tuple(r.values()))
    conn.commit()


def test_compute_and_store_uses_only_prior_28_days(conn):
    # Old, very different data outside the window must not shift the baseline.
    _insert(conn, _history(10, end="2026-10-01", hrv_ms=120))
    _insert(conn, [*_history(), {**NORMAL, "sleep_secs": 5 * 3600, "hrv_ms": 50}])
    result, stored = readiness.compute_and_store(conn, DAY)
    assert stored and result.status == "amber"
    row = derived.get_readiness(conn, DAY)
    assert row is not None and row["status"] == "amber"
    assert set(json.loads(row["flags_json"])) == {"hrv_low", "sleep_short"}
    assert json.loads(row["signals_json"])["missing"] == []


def test_final_row_is_not_overwritten(conn):
    _insert(conn, [*_history(), NORMAL])
    readiness.compute_and_store(conn, DAY, final=True)
    conn.execute("UPDATE wellness_day SET sleep_secs = 3600, hrv_ms = 40 WHERE date = ?", (DAY,))
    conn.commit()
    _, stored = readiness.compute_and_store(conn, DAY)
    assert stored is False
    row = derived.get_readiness(conn, DAY)
    assert row is not None and row["status"] == "green"


def test_dst_day_keeps_date_key(conn):
    dst = "2026-03-29"  # Europe clocks go forward
    _insert(conn, [*_history(end=dst), {**NORMAL, "date": dst}])
    result, _ = readiness.compute_and_store(conn, dst)
    assert result.day == dst and result.status == "green"
    assert derived.get_readiness(conn, dst) is not None
    assert derived.get_readiness(conn, "2026-03-28") is None
