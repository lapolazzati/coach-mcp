"""save_month_plan(): validate and store a month of sessions (U5, KTD11)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from store_helpers import TODAY, add_session, count, fresh_db, session

from coach.plan import month
from coach.plan.month import MonthPlanError, save_month_plan
from coach.plan.reconcile import build_payload

SS = "Warmup\n- 15m ramp 50-75%\n\nMain set 3x\n- 12m 88-93%\n- 5m 55%\n\nCooldown\n- 10m 50%"
Z2 = "- 10m ramp 50-65%\n- 1h20m 65-72% 90rpm\n- 10m 50%"
RUN = "- 10m Z1 HR\n- 35m Z2 HR"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


def s(day: str, slot: str = "am", sport: str = "virtual_ride", steps: str = Z2, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"date": day, "slot": slot, "sport": sport, "kind": "endurance", "name": f"{sport} {day}",
                           "duration_min": 100, "steps_text": steps}
    out.update(kw)
    return out


def save(conn: sqlite3.Connection, sessions: list[dict[str, Any]], m: str = "2026-12", **kw: Any) -> dict[str, Any]:
    return save_month_plan(conn, m, "base block", sessions, today=TODAY, **kw)


def december_18() -> list[dict[str, Any]]:
    """Mon/Wed/Thu/Sat rides, Tue run, Fri strength; Sundays and some days off."""
    out: list[dict[str, Any]] = []
    day = date(2026, 12, 1)
    while len(out) < 18:
        wd = day.weekday()
        iso = day.isoformat()
        if wd in (0, 3):
            out.append(s(iso, steps=SS, kind="sweet_spot", duration_min=70))
        elif wd == 1:
            out.append(s(iso, "pm", "run", RUN, kind="easy_run", duration_min=45))
        elif wd == 4:
            out.append(s(iso, "pm", "strength", "", kind="gym", duration_min=40))
        elif wd == 5:
            out.append(s(iso, sport="ride", start_time="09:30", duration_min=180,
                         steps="- 3h 60-70%"))
        day += timedelta(days=1)
    return out


def test_eighteen_session_month_saves(conn):
    plan = december_18()
    res = save(conn, plan)
    assert res == {"saved": 18, "warnings": [], "replaced": 0}
    assert count(conn, "SELECT 1 FROM planned_session WHERE date LIKE '2026-12-%'") == 18
    mp = conn.execute("SELECT rationale, plan_version FROM month_plan WHERE month = '2026-12'").fetchone()
    assert mp == ("base block", 1)
    row = session(conn, "coach:2026-12-03:am")
    assert row and row["sync_dirty"] == 1 and row["sport"] == "virtual_ride" and row["plan_version"] == 1
    stored = json.loads(str(row["session_json"]))
    assert stored["steps_text"] == SS and stored["steps"][1]["times"] == 3
    assert count(conn, "SELECT 1 FROM audit WHERE action = 'save_month_plan' AND actor = 'claude'") == 1


def test_saved_sessions_round_trip_to_intervals_payloads(conn):
    save(conn, december_18())
    rows = conn.execute("SELECT external_id FROM planned_session ORDER BY date").fetchall()
    payloads = {ext: build_payload(session(conn, ext)) for (ext,) in rows}  # type: ignore[arg-type]
    assert "Main set 3x\n- 12m 88-93%\n- 5m 55%" in payloads["coach:2026-12-03:am"]["description"]
    assert payloads["coach:2026-12-01:pm"]["type"] == "Run"
    assert payloads["coach:2026-12-04:pm"]["type"] == "WeightTraining"
    assert payloads["coach:2026-12-05:am"]["type"] == "Ride"


def test_slot_default_time_applied_and_start_time_kept(conn):
    save(conn, december_18())
    assert build_payload(session(conn, "coach:2026-12-03:am"))["start_date_local"] == "2026-12-03T07:00:00"  # type: ignore[arg-type]
    assert build_payload(session(conn, "coach:2026-12-01:pm"))["start_date_local"] == "2026-12-01T18:00:00"  # type: ignore[arg-type]
    assert build_payload(session(conn, "coach:2026-12-05:am"))["start_date_local"] == "2026-12-05T09:30:00"  # type: ignore[arg-type]
    assert month.SLOT_TIMES == {"am": "07:00", "pm": "18:00"}


def test_unparseable_steps_rejected_naming_the_line(conn):
    bad = s("2026-12-02", steps="- 10m 55%\n\n- 20 minutes hard")
    with pytest.raises(MonthPlanError) as exc:
        save(conn, [s("2026-12-01"), bad])
    msg = str(exc.value)
    assert "2026-12-02 am" in msg and "line 3" in msg and "- 20 minutes hard" in msg
    assert count(conn, "SELECT 1 FROM planned_session") == 0
    assert count(conn, "SELECT 1 FROM month_plan") == 0


def test_unknown_sport_rejected(conn):
    with pytest.raises(MonthPlanError, match="unknown sport 'swim'"):
        save(conn, [s("2026-12-01", sport="swim", steps="")])


def test_past_session_rejected(conn):
    with pytest.raises(MonthPlanError, match="2026-11-05 .*past"):
        save(conn, [s("2026-11-05"), s("2026-11-09")], m="2026-11")


def test_session_outside_month_rejected(conn):
    with pytest.raises(MonthPlanError, match="not in 2026-12"):
        save(conn, [s("2027-01-02")])


def test_two_sessions_in_one_slot_rejected(conn):
    with pytest.raises(MonthPlanError, match="2026-12-01 am.*more than one"):
        save(conn, [s("2026-12-01"), s("2026-12-01", sport="run", steps=RUN)])


def test_steps_must_fit_the_sport(conn):
    with pytest.raises(MonthPlanError, match="2026-12-01 am"):
        save(conn, [s("2026-12-01", steps="- 30m Z2 HR")])
    with pytest.raises(MonthPlanError, match="strength"):
        save(conn, [s("2026-12-01", sport="strength", steps="- 30m 50%")])


def test_ramp_warning_returned_but_plan_saved(conn):
    conn.execute("INSERT INTO wellness_day (date, ctl, atl, updated_at) VALUES (?, 40, 40, '2026-11-06T00:00:00Z')",
                 ((TODAY - timedelta(days=1)).isoformat(),))
    conn.commit()
    hard = "- 3h 85-95%"
    sessions = [s((date(2026, 11, 9) + timedelta(days=i)).isoformat(), steps=hard, duration_min=180)
                for i in range(6)]
    res = save(conn, sessions, m="2026-11")
    assert res["saved"] == 6
    assert any("CTL" in w for w in res["warnings"])
    assert count(conn, "SELECT 1 FROM planned_session") == 6


def test_no_rest_day_warning(conn):
    sessions = [s((date(2026, 12, 1) + timedelta(days=i)).isoformat(), steps="- 45m 55-65%", duration_min=45)
                for i in range(8)]
    res = save(conn, sessions)
    assert res["saved"] == 8
    assert any("rest" in w for w in res["warnings"])


def test_rest_day_rows_break_the_streak(conn):
    sessions = [s((date(2026, 12, 1) + timedelta(days=i)).isoformat(), steps="- 45m 55-65%", duration_min=45)
                for i in range(8)]
    sessions[3] = s("2026-12-04", sport="rest", steps="", duration_min=0)
    assert not any("rest" in w for w in save(conn, sessions)["warnings"])


def test_saving_over_a_month_keeps_locked_and_done_sessions(conn):
    locked = add_session(conn, "2026-12-03", user_locked=1, name="Lapo's group ride")
    done = add_session(conn, "2026-12-02", status="done", name="already done")
    same_slot = add_session(conn, "2026-12-10", name="old plan")
    dropped = add_session(conn, "2026-12-12", "pm", name="old plan pm")
    other_month = add_session(conn, "2027-01-04")
    res = save(conn, [s("2026-12-01"), s("2026-12-10", name="new plan")])
    assert res["saved"] == 2 and res["replaced"] == 2
    assert session(conn, locked)["name"] == "Lapo's group ride"  # type: ignore[index]
    assert session(conn, done)["name"] == "already done"  # type: ignore[index]
    assert session(conn, other_month) is not None
    new = session(conn, same_slot)
    assert new and new["name"] == "new plan" and new["sync_dirty"] == 1
    assert session(conn, dropped) is None
    assert count(conn, "SELECT 1 FROM session_tombstone WHERE external_id = ? AND pending_delete = 1",
                 (dropped,)) == 1
    again = save(conn, [s("2026-12-01")])
    assert again["replaced"] == 2
    assert conn.execute("SELECT plan_version FROM month_plan WHERE month = '2026-12'").fetchone()[0] == 2


def test_new_session_on_a_locked_slot_is_rejected(conn):
    add_session(conn, "2026-12-03", user_locked=1)
    with pytest.raises(MonthPlanError, match="2026-12-03 am.*locked"):
        save(conn, [s("2026-12-03")])


def test_job_actor_is_recorded(conn):
    save(conn, [s("2026-12-01")], actor="job:monthly")
    assert count(conn, "SELECT 1 FROM audit WHERE actor = 'job:monthly'") == 1


def test_canonical_sport_aliases():
    assert month.canonical_sport("Ride") == "ride"
    assert month.canonical_sport("VirtualRide") == "virtual_ride"
    assert month.canonical_sport("gym") == "strength"
    with pytest.raises(ValueError):
        month.canonical_sport("swim")
