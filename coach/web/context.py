"""Dashboard context from SQLite, in the shape of coach.web.sample_context.SAMPLE.

Every section reads real rows only. A section with nothing to show falls back
to the day-one shape (None or []) so the template renders its empty state; no
number is ever filled in to make a chart look complete. Goal, course and
targets are static inputs from coach.web.course.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from coach.web import course
from coach.web.charts import fmt_hm

# Readiness needs this many nights with both sleep and HRV.
READINESS_DAYS = 14
FITNESS_DAYS = 14
VOLUME_WEEKS = 6
WEIGHT_WINDOW_DAYS = 90
POWER_WINDOW_DAYS = 42  # "last 6 weeks" vs "6 weeks before" in the power table
RIDES_SHOWN = 5
CURVE = [("5s", 5), ("1m", 60), ("5m", 300), ("20m", 1200), ("60m", 3600)]

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
        # The planner (U8) owns sessions and plans; until it writes rows there is nothing to show.
        "session": None,
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
    return {"available": n >= READINESS_DAYS, "days_collected": n, "days_needed": READINESS_DAYS}


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
    rows = db.execute(
        f"SELECT {_DAY} AS day, sport_group, name, coalesce(start_local, start_utc) FROM activity"
        f" WHERE {_DAY} BETWEEN ? AND ? ORDER BY start_utc",
        (monday.isoformat(), (monday + timedelta(days=6)).isoformat()),
    ).fetchall()
    by_day: dict[str, list] = {}
    for day, group, name, start in rows:
        by_day.setdefault(day, []).append((group, name, start))

    week = []
    for i in range(7):
        d = monday + timedelta(days=i)
        acts = by_day.get(d.isoformat(), [])
        row: dict[str, object] = {"dow": f"{d:%a}", "date": str(d.day)}
        if acts:
            group = acts[0][0]
            row.update(
                kind={"cycling": "ride", "strength": "gym"}.get(group, group),
                title=" + ".join(name or g.capitalize() for g, name, _ in acts),
                slot=acts[0][2][11:16],
                status="done",
            )
        elif d < today:
            row.update(kind="rest", title="Nothing logged", slot="", status="rest")
        else:
            row.update(kind="rest", title="Not planned yet", slot="", status="planned")
        if d == today:
            row["today"] = True
        week.append(row)
    return week


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
    best20 = _best_watts(db, 1200, now_from, today)
    if best20 is None or not curve:
        return None
    # FTP estimated as 95% of the best 20 minutes until intervals.icu supplies one.
    ftp = round(best20 * 0.95)
    return {"ftp": ftp, "wkg": round(ftp / weight_kg, 2), "curve": curve}


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
