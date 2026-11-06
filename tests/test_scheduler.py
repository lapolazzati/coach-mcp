"""U7 scheduler wiring: triggers, DST, startup catch-up; weekly drift flags."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from store_helpers import TODAY, fresh_db

from coach.db.migrate import migrate
from coach.jobs import morning, scheduler, weekly
from coach.store import sync
from coach.store.db import TZ, connect_write, fetch_one

SETTINGS = SimpleNamespace(coach_tz="Europe/Rome")


def fires(trigger, start: datetime, end: datetime) -> list[datetime]:  # type: ignore[no-untyped-def]
    out: list[datetime] = []
    prev = None
    now = start
    while True:
        nxt = trigger.get_next_fire_time(prev, now)
        if nxt is None or nxt >= end:
            return out
        out.append(nxt)
        prev = nxt
        now = nxt + timedelta(seconds=1)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "coach.sqlite"
    conn = connect_write(path)
    migrate(conn)
    conn.close()
    return path


def build(db_path: Path, now: datetime):  # type: ignore[no-untyped-def]
    return scheduler.build_scheduler(SETTINGS, lambda: connect_write(db_path), lambda: None, now=now)


def test_jobs_and_defaults(db_path: Path) -> None:
    s = build(db_path, datetime(2026, 11, 6, 6, 40, tzinfo=TZ))
    ids = sorted(j.id for j in s.get_jobs())
    assert ids == ["backup", "morning", "morning_catchup", "sync", "weekly"]
    defaults = s._job_defaults  # applied to pending jobs when the scheduler starts
    assert (defaults["coalesce"], defaults["misfire_grace_time"], defaults["max_instances"]) == (True, 1800, 1)
    assert str(s.timezone) == "Europe/Rome"


def test_restart_at_0640_catches_up_once(db_path: Path) -> None:
    now = datetime(2026, 11, 6, 6, 40, tzinfo=TZ)
    first = build(db_path, now)
    catchups = [j for j in first.get_jobs() if j.id == "morning_catchup"]
    assert len(catchups) == 1
    assert catchups[0].trigger.run_date == now
    conn = connect_write(db_path)
    try:
        assert morning.run(conn, None, now)["done"]  # what the catch-up run does: no hard sessions ahead
        assert sync.get_state(conn, morning.state_key(TODAY)) == "done"
    finally:
        conn.close()
    again = build(db_path, now + timedelta(minutes=5))
    assert "morning_catchup" not in {j.id for j in again.get_jobs()}


def test_morning_window_every_ten_minutes() -> None:
    trig = scheduler.morning_trigger(TZ)
    day = datetime(2026, 11, 6, tzinfo=TZ)
    times = fires(trig, day, day + timedelta(days=1))
    assert times[0].strftime("%H:%M") == "05:30" and times[-1].strftime("%H:%M") == "17:50"
    assert len(times) == 75


@pytest.mark.parametrize("sunday", [date(2026, 10, 25), date(2026, 3, 29)])
def test_dst_sunday_fires_once(db_path: Path, sunday: date) -> None:
    s = build(db_path, datetime(2026, 11, 6, 6, 40, tzinfo=TZ))
    start = datetime(sunday.year, sunday.month, sunday.day, tzinfo=TZ) - timedelta(days=1)
    weekly_fires = fires(s.get_job("weekly").trigger, start, start + timedelta(days=2))
    assert [f.strftime("%Y-%m-%d %H:%M") for f in weekly_fires] == [f"{sunday.isoformat()} 20:00"]
    day = datetime(sunday.year, sunday.month, sunday.day, tzinfo=TZ)
    assert len(fires(scheduler.morning_trigger(TZ), day, day + timedelta(days=1))) == 75
    assert len(fires(s.get_job("backup").trigger, day, day + timedelta(days=1))) == 1


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


def test_weekly_flags_weight_rise(conn: sqlite3.Connection) -> None:
    monday = date(2026, 11, 2)
    for week, kg in ((-2, 70.0), (-1, 70.5), (0, 71.0)):
        for d in range(7):
            day = monday + timedelta(days=7 * week + d)
            conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?, ?, 'garmin', ?)",
                         (day.isoformat(), kg, "2026-11-01T00:00:00Z"))
    conn.commit()
    out = weekly.run(conn, datetime(2026, 11, 8, 20, 0, tzinfo=TZ))
    assert out["week_start"] == "2026-11-02"
    assert out["flags"] == ["weight_rising_2w"]
    row = fetch_one(conn, "SELECT metrics_json FROM weekly_summary WHERE week_start = '2026-11-02'")
    assert row is not None and json.loads(row["metrics_json"])["flags"] == ["weight_rising_2w"]


def test_weekly_flat_weight_no_flag(conn: sqlite3.Connection) -> None:
    for d in range(21):
        day = date(2026, 10, 19) + timedelta(days=d)
        conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?, 70.0, 'garmin', ?)",
                     (day.isoformat(), "2026-11-01T00:00:00Z"))
    conn.commit()
    assert weekly.run(conn, datetime(2026, 11, 8, 20, 0, tzinfo=TZ), dry_run=True)["flags"] == []
