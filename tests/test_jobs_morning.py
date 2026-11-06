"""U7 morning job: KTD9 deadlines, easing on amber/red, idempotency."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from store_helpers import TODAY, add_session, count, fresh_db, session
from test_reconcile import FakeClient

from coach.jobs import morning
from coach.store import sync
from coach.store.db import TZ

DAY = TODAY.isoformat()
GOOD = {"hrv_ms": None, "resting_hr": 48, "sleep_secs": 7 * 3600, "body_battery_high": 80}
RED = {"hrv_ms": 38, "resting_hr": 60, "sleep_secs": 4 * 3600 + 50 * 60, "body_battery_high": 80}


class WellnessClient(FakeClient):
    def get_wellness(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        self.calls.append(("wellness", (oldest, newest)))
        return []

    def list_activities(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        self.calls.append(("activities", (oldest, newest)))
        return []


def at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def put_wellness(conn: sqlite3.Connection, day: date, **values: Any) -> None:
    cols = {"date": day.isoformat(), "updated_at": "2026-11-01T00:00:00Z", **values}
    conn.execute(f"INSERT OR REPLACE INTO wellness_day ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()


def history(conn: sqlite3.Connection) -> None:
    """28 days of baseline: HRV 50/54 (mean 52, sd ~2), RHR 48, 7h sleep."""
    for i in range(1, 29):
        put_wellness(conn, TODAY - timedelta(days=i), **{**GOOD, "hrv_ms": 50 if i % 2 else 54})


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


@pytest.fixture
def client() -> WellnessClient:
    return WellnessClient()


@pytest.fixture
def red(conn: sqlite3.Connection) -> sqlite3.Connection:
    history(conn)
    put_wellness(conn, TODAY, **RED)
    return conn


def test_red_vo2_is_eased_and_reconciled(red: sqlite3.Connection, client: WellnessClient) -> None:
    ext = add_session(red, DAY, "pm", kind="vo2", name="VO2 5x3")
    out = morning.run(red, client, at(6))
    assert out["status"] == "red"
    assert out["eased"] == [ext]
    row = session(red, ext)
    assert row is not None
    assert (row["kind"], row["name"], row["duration_min"]) == ("endurance", "45 min Z2", 45)
    steps = json.loads(str(row["session_json"]))["steps"]
    assert (steps[0]["duration_s"], steps[0]["power_lo"], steps[0]["power_hi"]) == (2700, 60, 70)
    assert json.loads(str(row["eased_from_json"]))["kind"] == "vo2"
    reason = str(row["ease_reason"])
    assert "Sleep 4h50 (<5h30)" in reason and "HRV 38 vs baseline 52±2" in reason
    assert reason.endswith("→ swapped VO2 5x3 for 45 min Z2")
    assert ("upsert", [ext]) in client.calls
    assert ("wellness", (TODAY, TODAY)) in client.calls
    assert out["done"] and sync.get_state(red, morning.state_key(TODAY)) == "done"


def test_green_changes_nothing(conn: sqlite3.Connection) -> None:
    history(conn)
    put_wellness(conn, TODAY, **{**GOOD, "hrv_ms": 52})
    ext = add_session(conn, DAY, "pm", kind="vo2")
    out = morning.run(conn, None, at(6))
    assert out["status"] == "green" and out["eased"] == []
    assert session(conn, ext)["kind"] == "vo2"  # type: ignore[index]
    assert not out["done"]  # the 18:00 deadline is still ahead


def test_already_eased_session_is_skipped(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "pm", kind="vo2", eased_from_json=json.dumps({"kind": "vo2"}))
    out = morning.run(red, None, at(6))
    assert out["eased"] == []
    assert session(red, ext)["kind"] == "vo2"  # type: ignore[index]


def test_non_hard_kind_is_left_alone(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "pm", kind="Endurance")
    hard = add_session(red, DAY, "am", kind="VO2")
    out = morning.run(red, None, at(6))
    assert out["eased"] == [hard]
    assert session(red, ext)["kind"] == "Endurance"  # type: ignore[index]


def test_user_locked_gets_advice_once(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "pm", kind="threshold", user_locked=1)
    out = morning.run(red, None, at(6))
    assert out["advice"] == [ext] and out["eased"] == []
    assert session(red, ext)["kind"] == "threshold"  # type: ignore[index]
    again = morning.run(red, None, at(6, 10))
    assert again["advice"] == []
    assert count(red, "SELECT 1 FROM audit WHERE action = 'ease_advice'") == 1


def test_unknown_readiness_does_not_ease(conn: sqlite3.Connection) -> None:
    put_wellness(conn, TODAY, **RED)  # no baseline -> unknown
    ext = add_session(conn, DAY, "pm", kind="vo2")
    out = morning.run(conn, None, at(6))
    assert out["status"] == "unknown" and out["eased"] == []
    assert session(conn, ext)["kind"] == "vo2"  # type: ignore[index]


def test_past_deadline_does_not_ease(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "am", kind="vo2")
    out = morning.run(red, None, at(6, 55))
    assert out["too_late"] == [ext] and out["eased"] == []
    assert session(red, ext)["kind"] == "vo2"  # type: ignore[index]
    assert out["done"]


def test_sleep_arriving_at_0740_still_eases_evening_session(conn: sqlite3.Connection) -> None:
    history(conn)
    ext = add_session(conn, DAY, "pm", kind="intervals")
    first = morning.run(conn, None, at(7))
    assert first["status"] == "unknown" and not first["done"]
    put_wellness(conn, TODAY, **RED)
    second = morning.run(conn, None, at(7, 40))
    assert second["eased"] == [ext]
    assert session(conn, ext)["kind"] == "endurance"  # type: ignore[index]


def test_0700_session_decides_at_0650(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "am", kind="vo2")
    row = session(red, ext)
    assert row is not None
    assert morning.deadline(row) == at(6, 50)
    assert morning.run(red, None, at(6, 50))["too_late"] == [ext]
    assert morning.run(red, None, at(6, 49))["eased"] == [ext]


def test_late_start_deadline_is_ten_minutes_before() -> None:
    assert morning.deadline({"date": DAY, "slot": "pm", "start_time": "07:45"}) == at(7, 35)


def test_pinned_session_is_still_eased(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "pm", kind="tempo", pinned_date=1)
    assert morning.run(red, None, at(6))["eased"] == [ext]


def test_run_session_eases_to_hr_zone(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "pm", kind="threshold", sport="run")
    morning.run(red, None, at(6))
    step = json.loads(str(session(red, ext)["session_json"]))["steps"][0]  # type: ignore[index]
    assert step["hr_zone"] == 2 and "power_lo" not in step


def test_dry_run_proposes_without_writing(red: sqlite3.Connection) -> None:
    ext = add_session(red, DAY, "pm", kind="vo2")
    out = morning.run(red, None, at(6), dry_run=True)
    assert [p["external_id"] for p in out["proposals"]] == [ext]
    assert session(red, ext)["kind"] == "vo2"  # type: ignore[index]
    assert count(red, "SELECT 1 FROM readiness_day") == 0
    assert sync.get_state(red, morning.state_key(TODAY)) is None
