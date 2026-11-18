"""Dashboard context from SQLite, in the shape of coach.web.sample_context.SAMPLE.

Every section reads real rows only. A section with nothing to show falls back
to the day-one shape (None or []) so the template renders its empty state; no
number is ever filled in to make a chart look complete. Goal, course and
targets are static inputs from coach.web.course.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from coach.plan.month import SLOT_TIMES, SPORTS, _row_tss, canonical_sport
from coach.store import thresholds
from coach.store.db import Row, fetch_all, fetch_one
from coach.web import course
from coach.web.charts import fmt_hm

TZ = ZoneInfo("Europe/Rome")

# Readiness needs this many nights with both sleep and HRV.
READINESS_DAYS = 14
FITNESS_DAYS = 14
VOLUME_WEEKS = 6
WEIGHT_WINDOW_DAYS = 90
POWER_WINDOW_DAYS = 42  # "last 6 weeks" vs "6 weeks before" in the power table
RIDES_SHOWN = 5
NOTES_SHOWN = 3
CURVE = [("5s", 5), ("1m", 60), ("5m", 300), ("20m", 1200), ("60m", 3600)]

# Sessions of a day in clock order; a slot stands for its default time.
_SESSION_ORDER = "coalesce(start_time, CASE slot WHEN 'am' THEN '07:00' ELSE '18:00' END)"
# Local calendar date of an activity, falling back to UTC when the source had no wall time.
_DAY = "substr(coalesce(start_local, start_utc), 1, 10)"


def build_context(db: sqlite3.Connection, today: date) -> dict:
    monday = today - timedelta(days=today.weekday())
    weight = _weight(db, today)
    return {
        "sample": False,
        "today": {
            "label": _label(today),
            "week": min(max((today - course.PLAN_START).days // 7 + 1, 1), course.WEEKS_TOTAL),
            "weeks_total": course.WEEKS_TOTAL,
        },
        "session": _session(db, today),
        "readiness": _readiness(db, today),
        "goal": _goal(today),
        "course": {
            "profile": course.COURSE_PROFILE,
            "km_total": course.DISTANCE_KM,
            "positions_are_placeholders": course.POSITIONS_ARE_PLACEHOLDERS,
            "sectors": course.SECTORS,
            "fuel": course.FUEL,
        },
        "week": _week(db, monday, today),
        "fitness": _fitness(db, today),
        "weight": weight,
        "volume": _volume(db, monday),
        "adherence": None,
        "power": _power(db, today, weight["now"]),
        "rides": _rides(db, today),
        "review": _review(db, monday),
        "supplements": _supplements(db, today),
        "notes": _notes(db),
    }


def _label(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _readiness(db: sqlite3.Connection, today: date) -> dict:
    since = today - timedelta(days=READINESS_DAYS - 1)
    (n,) = db.execute(
        "SELECT count(*) FROM wellness_day WHERE date BETWEEN ? AND ?"
        " AND sleep_secs IS NOT NULL AND hrv_ms IS NOT NULL",
        (since.isoformat(), today.isoformat()),
    ).fetchone()
    row = fetch_one(db, "SELECT status, flags_json FROM readiness_day WHERE date = ?", (today.isoformat(),))
    return {
        "available": n >= READINESS_DAYS, "days_collected": n, "days_needed": READINESS_DAYS,
        "status": row["status"] if row else "unknown",
        "flags": [str(f) for f in _json(row["flags_json"], [])] if row else [],
    }


def _json(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _kind(sport: object) -> str:
    """Dashboard kind (ride, run, gym, rest) of a planned session's sport."""
    try:
        return SPORTS[canonical_sport(str(sport))][1] or "rest"
    except ValueError:
        return "rest"


def _local_hm(ts_utc: str) -> str:
    return datetime.fromisoformat(ts_utc).astimezone(TZ).strftime("%H:%M")


def _slot(row: Row) -> str:
    return str(row["start_time"] or SLOT_TIMES.get(str(row["slot"]), ""))


def _session(db: sqlite3.Connection, today: date) -> dict | None:
    """Today's first planned (or done) session, with the reason for any ease (R17, KTD9)."""
    row = fetch_one(
        db,
        "SELECT * FROM planned_session WHERE date = ? AND deleted_by_coach = 0 AND status IN ('planned', 'done')"
        f" ORDER BY status = 'done', {_SESSION_ORDER} LIMIT 1",
        (today.isoformat(),),
    )
    if row is None:
        return None
    tss = round(_row_tss(row))
    kind = _kind(row["sport"])
    minutes = int(row["duration_min"] or 0)
    eased = None
    if row["eased_at"]:
        eased = {"at": _local_hm(row["eased_at"]), "reason": row["ease_reason"] or ""}
    # A job cannot ease a session Lapo locked; it leaves advice in the audit log instead.
    advice = None
    note = fetch_one(
        db,
        "SELECT ts_utc, reason FROM audit WHERE action = 'ease_advice' AND entity = 'planned_session'"
        " AND entity_id = ? AND undone = 0 ORDER BY id DESC LIMIT 1",
        (str(row["id"]),),
    )
    if note is not None:
        advice = {"at": _local_hm(note["ts_utc"]), "reason": note["reason"] or ""}
    return {
        "kind": kind,
        "title": row["name"] or kind.capitalize(),
        "subtitle": str(row["kind"] or "").capitalize(),
        "slot": _slot(row),
        "duration_min": minutes,
        "tss": tss,
        "rating": min(max(round(tss / 25), 1), 5),
        "detail": f"{minutes} min" + (f" {row['kind']}" if row["kind"] else ""),
        "why": "",  # the ease reason shows in its own banner
        "status": row["status"],
        "in_zwift": bool(row["icu_event_id"]),
        "eased": eased,
        "advice": advice,
    }


def _goal(today: date) -> dict:
    return {
        "name": course.RACE_NAME,
        "route": course.RACE_ROUTE,
        "place": course.RACE_PLACE,
        "date": f"{course.RACE_DAY:%a} {course.RACE_DAY.day} {course.RACE_DAY:%b %Y}",
        "days_to_go": max((course.RACE_DAY - today).days, 0),
        "distance_km": course.DISTANCE_KM,
        "climb_m": course.CLIMB_M,
        "sectors": len(course.SECTORS),
        # The finish-time model is not built yet.
        "projected": None,
        "gap_min": None,
        "targets": course.TARGETS,
        "projected_history": [],
    }


def _week(db: sqlite3.Connection, monday: date, today: date) -> list[dict]:
    sunday = (monday + timedelta(days=6)).isoformat()
    rows = db.execute(
        f"SELECT {_DAY} AS day, sport_group, name, coalesce(start_local, start_utc) FROM activity"
        f" WHERE {_DAY} BETWEEN ? AND ? ORDER BY start_utc",
        (monday.isoformat(), sunday),
    ).fetchall()
    by_day: dict[str, list] = {}
    for day, group, name, start in rows:
        by_day.setdefault(day, []).append((group, name, start))
    plans: dict[str, list[Row]] = {}
    for p in fetch_all(
        db,
        "SELECT * FROM planned_session WHERE date BETWEEN ? AND ? AND deleted_by_coach = 0"
        f" ORDER BY date, {_SESSION_ORDER}",
        (monday.isoformat(), sunday),
    ):
        plans.setdefault(str(p["date"]), []).append(p)

    week = []
    for i in range(7):
        d = monday + timedelta(days=i)
        acts = by_day.get(d.isoformat(), [])
        plan = plans.get(d.isoformat(), [None])[0]
        row: dict[str, object] = {"dow": f"{d:%a}", "date": str(d.day)}
        if acts:
            group = acts[0][0]
            row.update(
                kind={"cycling": "ride", "strength": "gym"}.get(group, group),
                title=plan["name"] if plan and plan["name"] else " + ".join(
                    name or g.capitalize() for g, name, _ in acts),
                slot=acts[0][2][11:16],
                status="done",
            )
        elif plan is not None:
            row.update(_planned_row(plan, past=d < today))
        elif d < today:
            row.update(kind="rest", title="Nothing logged", slot="", status="rest")
        else:
            row.update(kind="rest", title="Not planned yet", slot="", status="planned")
        if plan is not None and plan["eased_at"] and "note" not in row:
            row["note"] = f"Eased: {plan['ease_reason']}" if plan["ease_reason"] else "Eased"
        if d == today:
            row["today"] = True
        week.append(row)
    return week


def _planned_row(plan: Row, *, past: bool) -> dict[str, object]:
    """A day with a planned session and no logged activity."""
    kind = _kind(plan["sport"])
    title = str(plan["name"] or kind.capitalize())
    status = str(plan["status"])
    if status == "planned" and past:
        status = "missed"  # the day is over and nothing was logged
    if status == "moved":
        return {"kind": kind, "title": title, "slot": "", "status": "moved", "note": "Moved"}
    if status == "skipped":
        return {"kind": kind, "title": f"Skipped: {title}", "slot": "", "status": "rest"}
    return {"kind": kind, "title": title, "slot": _slot(plan) if status == "planned" else "", "status": status}


def _fitness(db: sqlite3.Connection, today: date) -> dict | None:
    rows = db.execute(
        "SELECT ctl, atl FROM wellness_day WHERE date <= ? AND ctl IS NOT NULL AND atl IS NOT NULL"
        " ORDER BY date DESC LIMIT ?",
        (today.isoformat(), FITNESS_DAYS),
    ).fetchall()[::-1]
    if len(rows) < 2:
        return None
    ctl = [round(r[0]) for r in rows]
    atl = [round(r[1]) for r in rows]
    return {
        "ctl": ctl, "atl": atl,
        "ctl_now": ctl[-1], "atl_now": atl[-1], "tsb_now": ctl[-1] - atl[-1],
        "ctl_race_target": course.CTL_RACE_TARGET,
    }


def _weight(db: sqlite3.Connection, today: date) -> dict:
    # Several sources can log the same day; keep one value per date.
    rows = db.execute(
        "SELECT date, avg(kg) FROM weight WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
        ((today - timedelta(days=WEIGHT_WINDOW_DAYS)).isoformat(), today.isoformat()),
    ).fetchall()
    if not rows:
        # Day one: Lapo's stated starting weight is the only point.
        return {"series": [course.START_WEIGHT_KG], "now": course.START_WEIGHT_KG,
                "start": course.START_WEIGHT_KG, "target": course.TARGET_WEIGHT_KG, "per_week": None}
    series = [round(kg, 1) for _, kg in rows]
    return {
        "series": series,
        "now": series[-1],
        "start": series[0],
        "target": course.TARGET_WEIGHT_KG,
        "per_week": _slope_per_week(rows),
    }


def _slope_per_week(rows: list[tuple[str, float]]) -> float | None:
    """Least-squares kg per week; None until the weigh-ins span two weeks."""
    days = [date.fromisoformat(d).toordinal() for d, _ in rows]
    if len(rows) < 2 or days[-1] - days[0] < 14:
        return None
    kgs = [kg for _, kg in rows]
    mx, my = sum(days) / len(days), sum(kgs) / len(kgs)
    den = sum((x - mx) ** 2 for x in days)
    return round(sum((x - mx) * (y - my) for x, y in zip(days, kgs)) / den * 7, 2)


def _volume(db: sqlite3.Connection, monday: date) -> list[dict]:
    first = monday - timedelta(weeks=VOLUME_WEEKS - 1)
    rows = dict(db.execute(
        f"SELECT {_DAY} AS day, sum(coalesce(duration_s, moving_s, 0)) FROM activity"
        f" WHERE sport_group = 'cycling' AND {_DAY} BETWEEN ? AND ? GROUP BY day",
        (first.isoformat(), (monday + timedelta(days=6)).isoformat()),
    ).fetchall())
    weeks = []
    for w in range(VOLUME_WEEKS):
        start = first + timedelta(weeks=w)
        secs = sum(rows.get((start + timedelta(days=i)).isoformat(), 0) for i in range(7))
        weeks.append({"label": f"W{start.isocalendar().week}", "planned": None,
                      "done": round(secs / 3600, 1)})
    return weeks if any(w["done"] for w in weeks) else []


def _best_watts(db: sqlite3.Connection, duration_s: int, since: date, until: date) -> float | None:
    (w,) = db.execute(
        "SELECT max(b.watts) FROM activity_power_best b JOIN activity a ON a.id = b.activity_id"
        " WHERE b.duration_s = ? AND substr(coalesce(a.start_local, a.start_utc), 1, 10) BETWEEN ? AND ?",
        (duration_s, since.isoformat(), until.isoformat()),
    ).fetchone()
    if w is None and duration_s == 1200:
        # Garmin's activity summary carries the 20-minute best even without a FIT stream.
        (w,) = db.execute(
            f"SELECT max(max_20min_power) FROM activity WHERE {_DAY} BETWEEN ? AND ?",
            (since.isoformat(), until.isoformat()),
        ).fetchone()
    return w


def _power(db: sqlite3.Connection, today: date, weight_kg: float) -> dict | None:
    now_from = today - timedelta(days=POWER_WINDOW_DAYS - 1)
    prev_from = now_from - timedelta(days=POWER_WINDOW_DAYS)
    prev_to = now_from - timedelta(days=1)
    curve = []
    for label, secs in CURVE:
        now = _best_watts(db, secs, now_from, today)
        prev = _best_watts(db, secs, prev_from, prev_to)
        if now is not None and prev is not None:
            curve.append({"dur": label, "now": round(now), "prev": round(prev)})
    manual = thresholds.effective(db, today).get("ftp_w")
    if manual:
        ftp, source = round(manual["value"]), "manual"
    else:
        best20 = _best_watts(db, 1200, now_from, today)
        if best20 is None or not curve:
            return None
        # no FTP set (set_athlete_settings): estimate it as 95% of the best 20 minutes
        ftp, source = round(best20 * 0.95), "estimate"
    return {"ftp": ftp, "ftp_source": source, "wkg": round(ftp / weight_kg, 2), "curve": curve}


def _rides(db: sqlite3.Connection, today: date) -> list[dict]:
    rows = db.execute(
        f"SELECT {_DAY} AS day, name, distance_m, coalesce(duration_s, moving_s), norm_power FROM activity"
        f" WHERE sport_group = 'cycling' AND {_DAY} <= ? ORDER BY start_utc DESC LIMIT ?",
        (today.isoformat(), RIDES_SHOWN),
    ).fetchall()
    return [
        {
            "date": _label(date.fromisoformat(day)),
            "title": name or "Ride",
            "km": round((dist or 0) / 1000, 1),
            "time": fmt_hm((secs or 0) / 60),
            "np": round(np) if np else None,
            "segments": [],
        }
        for day, name, dist, secs, np in rows
    ]


def _review(db: sqlite3.Connection, monday: date) -> dict | None:
    """The latest weekly review: Claude's prose when saved, the job's numbers always (KTD12)."""
    weeks = fetch_one(
        db,
        "SELECT max(week_start) AS w FROM (SELECT week_start FROM weekly_summary UNION ALL"
        " SELECT week_start FROM weekly_review) WHERE week_start <= ?",
        (monday.isoformat(),),
    )
    if weeks is None or weeks["w"] is None:
        return None
    week = str(weeks["w"])
    summary = fetch_one(db, "SELECT metrics_json FROM weekly_summary WHERE week_start = ?", (week,))
    prose = fetch_one(db, "SELECT text FROM weekly_review WHERE week_start = ?", (week,))
    metrics = _json(summary["metrics_json"], {}) if summary else {}
    start = date.fromisoformat(week)
    return {
        "week": f"W{start.isocalendar().week} · from {_label(start)}",
        "prose": prose["text"] if prose else None,
        "numbers": [
            {"label": str(k).replace("_", " ").capitalize(), "value": _fmt_metric(v)}
            for k, v in (metrics.items() if isinstance(metrics, dict) else [])
            if isinstance(v, (int, float, str)) and not isinstance(v, bool)
        ],
    }


def _fmt_metric(v: float | str) -> str:
    if isinstance(v, float):
        return f"{v:.1f}".removesuffix(".0")
    return str(v)


def _supplements(db: sqlite3.Connection, today: date) -> list[dict]:
    rows = fetch_all(
        db,
        "SELECT s.name, s.dose, s.timing, EXISTS (SELECT 1 FROM supplement_intake i"
        " WHERE i.supplement_id = s.id AND i.local_date = ?) AS taken"
        " FROM supplement s WHERE s.active = 1"
        " ORDER BY CASE s.timing WHEN 'morning' THEN 0 WHEN 'lunch' THEN 1 WHEN 'with_meal' THEN 2 ELSE 3 END, s.name",
        (today.isoformat(),),
    )
    return [
        {"name": r["name"], "dose": r["dose"] or "", "timing": str(r["timing"]).replace("_", " "),
         "taken": bool(r["taken"])}
        for r in rows
    ]


def _notes(db: sqlite3.Connection) -> list[dict]:
    rows = fetch_all(db, "SELECT local_date, text, tags FROM note ORDER BY ts_utc DESC, id DESC LIMIT ?",
                     (NOTES_SHOWN,))
    return [
        {"date": _label(date.fromisoformat(r["local_date"])), "text": r["text"],
         "tags": [t for t in (r["tags"] or "").split(",") if t]}
        for r in rows
    ]
