"""Monthly plan: validate Claude's month of sessions and save it (KTD11).

save_month_plan() rejects the whole month on any hard error (bad steps text,
past date, two sessions in a slot, unknown sport, steps that don't fit the
sport) and warns, but still saves, on a weekly CTL ramp above 7 or seven days
in a row without rest. The write itself is coach.store.plan_ops.save_month
(KTD8); reconcile() pushes the dirty rows afterwards.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from coach.plan import steps_text
from coach.store import plan_ops, sync
from coach.store.db import Row, fetch_all, fetch_one, today_local
from coach.workouts.model import Repeat, Session, Step

SLOT_TIMES = sync.SLOTS

# The sport vocabulary, in one place: canonical name -> (intervals.icu type, model sport, indoor).
# rest is never pushed.
SPORTS: dict[str, tuple[str | None, str | None, bool]] = {
    "ride": ("Ride", "ride", False),
    "virtual_ride": ("VirtualRide", "ride", True),
    "run": ("Run", "run", False),
    "strength": ("WeightTraining", "gym", True),
    "rest": (None, None, False),
}
# Older rows and other spellings.
SPORT_ALIASES = {"Ride": "ride", "VirtualRide": "virtual_ride", "Run": "run", "gym": "strength",
                 "WeightTraining": "strength", "Rest": "rest"}

RAMP_LIMIT = 7.0  # CTL points per week
MAX_DAYS_WITHOUT_REST = 6
CTL_DAYS = 42
HR_ZONE_IF = {1: 0.6, 2: 0.7, 3: 0.8, 4: 0.9, 5: 1.0}
PACE_IF = 0.75
DEFAULT_IF = {"ride": 0.7, "virtual_ride": 0.7, "run": 0.7, "strength": 0.5, "rest": 0.0}
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class MonthPlanError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("month plan rejected:\n" + "\n".join(f"- {p}" for p in problems))


class PlannedInput(BaseModel):
    date: date
    slot: str
    sport: str
    kind: str
    name: str
    duration_min: int = Field(ge=0)
    steps_text: str = ""
    start_time: str | None = None


def canonical_sport(value: str) -> str:
    sport = SPORT_ALIASES.get(value, value)
    if sport not in SPORTS:
        raise ValueError(f"unknown sport {value!r}; use one of {', '.join(SPORTS)}")
    return sport


def _month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(f"{month}-01")
    nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first, nxt


def _check(raw: dict[str, Any], month: str, first: date, nxt: date, today: date,
           problems: list[str]) -> tuple[PlannedInput, list[Step | Repeat]] | None:
    where = f"{raw.get('date')} {raw.get('slot')}"
    try:
        item = PlannedInput.model_validate(raw)
    except ValidationError as exc:
        problems.append(f"{where}: {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}")
        return None
    before = len(problems)
    if item.slot not in SLOT_TIMES:
        problems.append(f"{where}: unknown slot {item.slot!r}; use {' or '.join(SLOT_TIMES)}")
    if not first <= item.date < nxt:
        problems.append(f"{where}: not in {month}")
    if item.date < today:
        problems.append(f"{where}: {item.date.isoformat()} is in the past")
    if item.start_time is not None and not _CLOCK.match(item.start_time):
        problems.append(f"{where}: start_time {item.start_time!r} is not HH:MM")
    try:
        sport = canonical_sport(item.sport)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
        return None
    try:
        steps = steps_text.parse(item.steps_text)
    except steps_text.StepsTextError as exc:
        problems.append(f"{where}: steps {exc}")
        return None
    model_sport = SPORTS[sport][1]
    if model_sport in (None, "gym") and steps:
        problems.append(f"{where}: {sport} sessions take no steps; put the content in the name or drop steps_text")
    elif model_sport is not None:
        try:
            Session(external_id="check", sport=model_sport, start=datetime.combine(item.date, time(7)),  # type: ignore[arg-type]
                    name=item.name, steps=steps, duration_s=item.duration_min * 60 or None)
        except ValidationError as exc:
            problems.append(f"{where}: {exc.errors()[0]['msg']}")
    if len(problems) > before:
        return None
    return item.model_copy(update={"sport": sport}), steps


def save_month_plan(conn: sqlite3.Connection, month: str, rationale: str, sessions: list[dict[str, Any]], *,
                    actor: str = "claude", today: date | None = None) -> dict[str, Any]:
    """Validate and save a month. Raises MonthPlanError listing every hard problem; returns
    {saved, warnings, replaced}."""
    today = today or today_local()
    try:
        first, nxt = _month_bounds(month)
    except ValueError as exc:
        raise MonthPlanError([f"month {month!r} is not YYYY-MM"]) from exc
    problems: list[str] = []
    checked: list[tuple[PlannedInput, list[Step | Repeat]]] = []
    seen: set[tuple[date, str]] = set()
    for raw in sessions:
        ok = _check(raw, month, first, nxt, today, problems)
        if ok is None:
            continue
        key = (ok[0].date, ok[0].slot)
        if key in seen:
            problems.append(f"{key[0].isoformat()} {key[1]}: more than one session in this slot")
            continue
        seen.add(key)
        checked.append(ok)
    if problems:
        raise MonthPlanError(problems)

    rows = [{
        "date": item.date.isoformat(), "slot": item.slot, "start_time": item.start_time, "sport": item.sport,
        "kind": item.kind, "name": item.name, "duration_min": item.duration_min,
        "session_json": json.dumps({"steps": [s.model_dump(mode="json", exclude_none=True) for s in steps],
                                    "steps_text": item.steps_text}),
    } for item, steps in checked]
    res = plan_ops.save_month(conn, month, rationale, rows, actor=actor, today=today)
    if res.status != "ok":
        raise MonthPlanError([res.message])
    return {"saved": len(rows), "warnings": plan_warnings(conn, today, first, nxt),
            "replaced": res.replaced}


# --- warnings -----------------------------------------------------------------

def _step_if(step: Step, sport: str) -> float:
    if step.power_lo is not None:
        return (step.power_lo + (step.power_hi or step.power_lo)) / 200
    if step.hr_zone is not None:
        return HR_ZONE_IF[step.hr_zone]
    if step.pace_s_per_km is not None:
        return PACE_IF
    return DEFAULT_IF[sport]


def estimate_tss(sport: str, duration_min: int, steps: list[Step | Repeat]) -> float:
    """Rough TSS: sum of hours x IF^2 x 100 per step; unstructured sessions use a default IF."""
    if sport == "rest":
        return 0.0
    if not steps:
        return duration_min / 60 * DEFAULT_IF[sport] ** 2 * 100
    total = 0.0
    for item in steps:
        times, flat = (item.times, item.steps) if isinstance(item, Repeat) else (1, [item])
        total += times * sum(st.duration_s / 3600 * _step_if(st, sport) ** 2 * 100 for st in flat)
    return total


def _row_tss(row: Row) -> float:
    try:
        sport = canonical_sport(str(row["sport"]))
        steps = [] if SPORTS[sport][1] in (None, "gym") else steps_text.stored_steps(row.get("session_json"))
    except ValueError:
        return 0.0
    return estimate_tss(sport, int(row.get("duration_min") or 0), steps)


def plan_warnings(conn: sqlite3.Connection, today: date, first: date, nxt: date) -> list[str]:
    """Warnings over the month as saved: runs of days without rest, and weekly CTL ramp."""
    start = max(first, today)
    rows = fetch_all(conn, "SELECT * FROM planned_session WHERE date >= ? AND date < ? AND status != 'skipped'",
                     ((start - timedelta(days=CTL_DAYS)).isoformat(), nxt.isoformat()))
    tss: dict[date, float] = {}
    training: set[date] = set()
    for row in rows:
        day = date.fromisoformat(str(row["date"]))
        tss[day] = tss.get(day, 0.0) + _row_tss(row)
        if SPORT_ALIASES.get(str(row["sport"]), str(row["sport"])) != "rest":
            training.add(day)
    warnings: list[str] = []

    streak: list[date] = []
    for offset in range((nxt - start).days):
        day = start + timedelta(days=offset)
        streak = [*streak, day] if day in training else []
        if len(streak) == MAX_DAYS_WITHOUT_REST + 1:
            warnings.append(f"no rest day from {streak[0].isoformat()}: {len(streak)}+ training days in a row")

    base = fetch_one(conn, "SELECT date, ctl FROM wellness_day WHERE ctl IS NOT NULL AND date <= ?"
                           " ORDER BY date DESC LIMIT 1", (today.isoformat(),))
    if base is None:
        return warnings
    day, ctl = date.fromisoformat(str(base["date"])), float(base["ctl"])
    ctl_by_day = {day: ctl}
    while day < nxt:
        day += timedelta(days=1)
        ctl += (tss.get(day, 0.0) - ctl) / CTL_DAYS
        ctl_by_day[day] = ctl
    week = start - timedelta(days=start.weekday())  # the Monday of the first week
    while week < nxt:
        before, end = week - timedelta(days=1), min(week + timedelta(days=6), nxt - timedelta(days=1))
        if before in ctl_by_day and end in ctl_by_day:
            rise = ctl_by_day[end] - ctl_by_day[before]
            if rise > RAMP_LIMIT:
                warnings.append(f"week of {week.isoformat()}: CTL +{rise:.1f}, above {RAMP_LIMIT:g}/week")
        week += timedelta(days=7)
    return warnings
