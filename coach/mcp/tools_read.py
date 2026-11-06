"""U9: coach-mcp read tools. Every tool opens a read-only connection.

Dates are ISO strings; defaults are relative to today in Europe/Rome. Every
result carries ``as_of`` and ``freshness`` and stays under the size cap
(see ``coach.mcp.format``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from coach.mcp.format import (
    Clock,
    Result,
    connect_ro,
    date_range,
    envelope,
    error,
    parse_day,
    utc_now,
)
from coach.store.db import fetch_all, fetch_one, today_local
from coach.store.logs import weight_mean_7d
from coach.web.context import _DAY, CURVE, _best_watts

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

TREND_METRICS = ("ctl", "atl", "form", "weight", "hrv", "resting_hr", "sleep_h", "load", "hours", "power_bests")
# Daily values summed per bucket; the rest are averaged.
SUMMED = {"load", "hours"}
MAX_WEEK_BUCKETS = 53
CURRENT_BESTS_DAYS = 42

Today = Callable[[], date]


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def _session(row: Result) -> Result:
    out: Result = {k: row[k] for k in ("external_id", "date", "slot", "start_time", "sport", "kind", "name",
                               "duration_min", "status", "user_locked", "pinned_date", "sync_dirty",
                               "ease_reason", "eased_at") if k in row}
    content = _loads(row.get("session_json"))
    if isinstance(content, dict):
        out["steps_text"] = content.get("steps_text") or None
        if not out["steps_text"]:
            out["steps"] = content.get("steps")
    out["sync"] = "pending" if row.get("sync_dirty") else "synced"
    return out


def _advice(conn: sqlite3.Connection, session_id: int) -> list[Result]:
    rows = fetch_all(conn, "SELECT ts_utc, actor, reason, after_json FROM audit WHERE action = 'ease_advice'"
                           " AND entity = 'planned_session' AND entity_id = ? AND undone = 0 ORDER BY id",
                     (str(session_id),))
    return [{"ts_utc": r["ts_utc"], "by": r["actor"], "reason": r["reason"], "proposed": _loads(r["after_json"])}
            for r in rows]


def _sessions(conn: sqlite3.Connection, start: date, end: date) -> list[Result]:
    rows = fetch_all(conn, "SELECT * FROM planned_session WHERE date BETWEEN ? AND ? ORDER BY date, slot",
                     (start.isoformat(), end.isoformat()))
    return [_session(r) for r in rows]


def _readiness(conn: sqlite3.Connection, day: date) -> Result:
    row = fetch_one(conn, "SELECT * FROM readiness_day WHERE date = ?", (day.isoformat(),))
    if row is None:
        return {"status": "unknown", "reasons": ["readiness not computed yet for today"], "final": False}
    flags = _loads(row.get("flags_json")) or []
    signals = _loads(row.get("signals_json")) or {}
    reasons = list(flags) if flags else ["no flags"]
    return {"status": row["status"], "reasons": reasons, "signals": signals, "final": bool(row.get("final")),
            "computed_at": row.get("computed_at")}


def _meals(conn: sqlite3.Connection, start: date, end: date) -> list[Result]:
    return fetch_all(conn, "SELECT id, local_date, ts_utc, description, kcal, carbs_g, protein_g, fat_g, source,"
                           " confidence, confirmed FROM meal WHERE local_date BETWEEN ? AND ? ORDER BY ts_utc",
                     (start.isoformat(), end.isoformat()))


def _kcal(meals: list[Result]) -> float:
    return round(sum(m["kcal"] or 0 for m in meals), 1)


def _supplements(conn: sqlite3.Connection, day: date) -> list[Result]:
    sups = fetch_all(conn, "SELECT id, name, dose, timing, active, notes FROM supplement ORDER BY active DESC, name")
    taken = fetch_all(conn, "SELECT supplement_id, timing, ts_utc FROM supplement_intake WHERE local_date = ?",
                      (day.isoformat(),))
    by_sup: dict[int, list[Result]] = {}
    for t in taken:
        by_sup.setdefault(int(t["supplement_id"]), []).append(t)
    out = []
    for s in sups:
        intakes = by_sup.get(int(s["id"]), [])
        out.append({**s, "active": bool(s["active"]), "taken_today": bool(intakes),
                    "taken_at": [t["ts_utc"] for t in intakes]})
    return out


def _bucket_key(day: date, bucket: str) -> str:
    if bucket == "month":
        return day.strftime("%Y-%m")
    return (day - timedelta(days=day.weekday())).isoformat()


def _slope(values: list[float | None]) -> float | None:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return None
    mx = sum(i for i, _ in pts) / len(pts)
    my = sum(v for _, v in pts) / len(pts)
    den = sum((i - mx) ** 2 for i, _ in pts)
    return round(sum((i - mx) * (v - my) for i, v in pts) / den, 3) if den else None


def _daily_series(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict[str, float]]:
    """metric -> {iso day: value} for every trend metric except power_bests."""
    s, e = start.isoformat(), end.isoformat()
    out: dict[str, dict[str, float]] = {m: {} for m in TREND_METRICS if m != "power_bests"}
    for day, ctl, atl, hrv, rhr, sleep in conn.execute(
        "SELECT date, ctl, atl, hrv_ms, resting_hr, sleep_secs FROM wellness_day WHERE date BETWEEN ? AND ?", (s, e)
    ):
        for metric, value in (("ctl", ctl), ("atl", atl), ("hrv", hrv), ("resting_hr", rhr)):
            if value is not None:
                out[metric][day] = float(value)
        if ctl is not None and atl is not None:
            out["form"][day] = float(ctl) - float(atl)
        if sleep is not None:
            out["sleep_h"][day] = float(sleep) / 3600
    for day, kg in conn.execute("SELECT date, avg(kg) FROM weight WHERE date BETWEEN ? AND ? GROUP BY date", (s, e)):
        out["weight"][day] = float(kg)
    for day, load, secs in conn.execute(
        f"SELECT {_DAY} AS day, sum(coalesce(training_load, 0)), sum(coalesce(duration_s, moving_s, 0))"
        f" FROM activity WHERE {_DAY} BETWEEN ? AND ? GROUP BY day", (s, e)
    ):
        out["load"][day] = float(load)
        out["hours"][day] = float(secs) / 3600
    return out


def _power_bests(conn: sqlite3.Connection, start: date, end: date) -> Result:
    recent = max(start, end - timedelta(days=CURRENT_BESTS_DAYS - 1))

    def bests(since: date) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for label, secs in CURVE:
            w = _best_watts(conn, secs, since, end)
            out[label] = round(w) if w is not None else None
        return out

    return {"current": bests(recent), "current_since": recent.isoformat(),
            "window": bests(start), "window_since": start.isoformat()}


def trends(conn: sqlite3.Connection, today: date, metrics: list[str], months: int, bucket: str) -> Result:
    unknown = [m for m in metrics if m not in TREND_METRICS]
    if unknown or not metrics:
        return error(f"unknown metric(s) {unknown or metrics}; pick from {list(TREND_METRICS)}")
    if bucket not in ("week", "month"):
        return error("bucket must be 'week' or 'month'")
    if not 1 <= months <= 24:
        return error("months must be between 1 and 24")
    if bucket == "week":
        weeks = min(MAX_WEEK_BUCKETS, -(-months * 365 // 12 // 7))
        start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
        keys = [(start + timedelta(weeks=i)).isoformat() for i in range(weeks)]
    else:
        y, m = today.year, today.month - (months - 1)
        while m < 1:
            y, m = y - 1, m + 12
        start = date(y, m, 1)
        keys = []
        d = start
        while d <= today:
            keys.append(d.strftime("%Y-%m"))
            d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    series = _daily_series(conn, start, today)
    rows: list[Result] = [{"bucket": k} for k in keys]
    slopes: Result = {}
    for metric in metrics:
        if metric == "power_bests":
            continue
        grouped: dict[str, list[float]] = {}
        for day, value in series[metric].items():
            grouped.setdefault(_bucket_key(date.fromisoformat(day), bucket), []).append(value)
        values: list[float | None] = []
        for row in rows:
            vals = grouped.get(row["bucket"])
            agg = None if not vals else (sum(vals) if metric in SUMMED else sum(vals) / len(vals))
            row[metric] = None if agg is None else round(agg, 2)
            values.append(row[metric])
        slopes[metric] = _slope(values)
    out: Result = {"start": start.isoformat(), "end": today.isoformat(), "bucket": bucket, "buckets": rows,
                   "slope_per_bucket": slopes,
                   "aggregation": {m: ("sum" if m in SUMMED else "mean") for m in metrics if m != "power_bests"}}
    if "power_bests" in metrics:
        out["power_bests"] = _power_bests(conn, start, today)
    return out


def register(mcp: FastMCP, db_path: Path | str, *, today: Today = today_local, now: Clock = utc_now) -> None:
    """Add the read tools to `mcp`. `today`/`now` are injectable for tests."""

    @contextmanager
    def ro() -> Iterator[sqlite3.Connection]:
        conn = connect_ro(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def reply(conn: sqlite3.Connection, data: Result) -> Result:
        return data if "error" in data else envelope(conn, data, now())

    @mcp.tool(annotations=READ_ONLY)
    def get_today() -> Result:
        """Start here for "how am I today": readiness with reasons, today's session, supplements, meals, weight."""
        day = today()
        with ro() as conn:
            rows = fetch_all(conn, "SELECT * FROM planned_session WHERE date = ? AND sport != 'rest'"
                                   " ORDER BY slot", (day.isoformat(),))
            sessions = []
            for row in rows:
                s = _session(row)
                s["advice"] = _advice(conn, int(row["id"]))
                sessions.append(s)
            meals = _meals(conn, day, day)
            latest = fetch_one(conn, "SELECT date, kg, source FROM weight WHERE date <= ?"
                                     " ORDER BY date DESC, updated_at DESC LIMIT 1", (day.isoformat(),))
            mean = weight_mean_7d(conn, (day + timedelta(days=1)).isoformat())
            data: Result = {
                "date": day.isoformat(),
                "label": sessions[0]["name"] if sessions else "rest day",
                "readiness": _readiness(conn, day),
                "session": sessions[0] if sessions else None,
                "other_sessions": sessions[1:],
                "supplements": _supplements(conn, day),
                "meals": {"items": meals, "kcal_total": _kcal(meals)},
                "weight": {"latest": latest, "mean_7d": None if mean is None else round(mean, 2)},
            }
            return reply(conn, data)

    @mcp.tool(annotations=READ_ONLY)
    def get_wellness(start: str | None = None, end: str | None = None) -> Result:
        """Daily sleep, HRV, resting HR, body battery, steps, CTL/ATL between two dates (default: last 14 days)."""
        day = today()
        rng = date_range(start, end, day - timedelta(days=13), day)
        if isinstance(rng, str):
            return error(rng)
        with ro() as conn:
            rows = fetch_all(conn, "SELECT * FROM wellness_day WHERE date BETWEEN ? AND ? ORDER BY date",
                             (rng[0].isoformat(), rng[1].isoformat()))
            return reply(conn, {"start": rng[0].isoformat(), "end": rng[1].isoformat(), "days": rows})

    @mcp.tool(annotations=READ_ONLY)
    def get_training(start: str | None = None, end: str | None = None) -> Result:
        """Done activities and planned sessions with status between two dates (default: last 7 to next 7 days)."""
        day = today()
        rng = date_range(start, end, day - timedelta(days=7), day + timedelta(days=7))
        if isinstance(rng, str):
            return error(rng)
        s, e = rng
        with ro() as conn:
            acts = fetch_all(
                conn,
                f"SELECT id, {_DAY} AS date, sport, sport_group, name, start_local, duration_s, moving_s,"
                " distance_m, elevation_gain_m, avg_hr, avg_power, norm_power, training_load FROM activity"
                f" WHERE {_DAY} BETWEEN ? AND ? ORDER BY start_utc", (s.isoformat(), e.isoformat()))
            return reply(conn, {"start": s.isoformat(), "end": e.isoformat(), "activities": acts,
                                "planned": _sessions(conn, s, e)})

    @mcp.tool(annotations=READ_ONLY)
    def get_trends(metrics: list[str], months: int = 6, bucket: Literal["week", "month"] = "week") -> Result:
        """Long-range trends: per-week or per-month values and slope (ctl, atl, form, weight, power_bests, ...)."""
        with ro() as conn:
            return reply(conn, trends(conn, today(), metrics, months, bucket))

    @mcp.tool(annotations=READ_ONLY)
    def get_plan(month: str | None = None) -> Result:
        """The month plan (YYYY-MM, default this month): the rationale and every planned session."""
        day = today()
        month = month or day.strftime("%Y-%m")
        first = parse_day(f"{month}-01", day, "month")
        if isinstance(first, str):
            return error(f"month must be YYYY-MM, got {month!r}")
        nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        with ro() as conn:
            plan = fetch_one(conn, "SELECT rationale, plan_version FROM month_plan WHERE month = ?", (month,))
            return reply(conn, {"month": month, "rationale": plan["rationale"] if plan else None,
                                "plan_version": plan["plan_version"] if plan else None,
                                "sessions": _sessions(conn, first, nxt - timedelta(days=1))})

    @mcp.tool(annotations=READ_ONLY)
    def get_notes(start: str | None = None, end: str | None = None, tag: str | None = None) -> Result:
        """Lapo's notes between two dates (default: last 30 days), optionally only those with one tag."""
        day = today()
        rng = date_range(start, end, day - timedelta(days=29), day)
        if isinstance(rng, str):
            return error(rng)
        with ro() as conn:
            rows = fetch_all(conn, "SELECT id, local_date, ts_utc, text, tags, source FROM note"
                                   " WHERE local_date BETWEEN ? AND ? ORDER BY ts_utc",
                             (rng[0].isoformat(), rng[1].isoformat()))
            notes = []
            for r in rows:
                tags = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
                if tag is None or tag.strip().lower() in {t.lower() for t in tags}:
                    notes.append({**r, "tags": tags})
            return reply(conn, {"start": rng[0].isoformat(), "end": rng[1].isoformat(), "tag": tag,
                                "notes": notes})

    @mcp.tool(annotations=READ_ONLY)
    def get_meals(start: str | None = None, end: str | None = None) -> Result:
        """Logged meals with per-day kcal and macro totals between two dates (default: last 7 days)."""
        day = today()
        rng = date_range(start, end, day - timedelta(days=6), day)
        if isinstance(rng, str):
            return error(rng)
        with ro() as conn:
            meals = _meals(conn, rng[0], rng[1])
            totals: dict[str, Result] = {}
            for m in meals:
                t = totals.setdefault(m["local_date"], {"date": m["local_date"], "kcal": 0.0, "carbs_g": 0.0,
                                                        "protein_g": 0.0, "fat_g": 0.0, "meals": 0})
                t["meals"] += 1
                for k in ("kcal", "carbs_g", "protein_g", "fat_g"):
                    t[k] = round(t[k] + (m[k] or 0), 1)
            return reply(conn, {"start": rng[0].isoformat(), "end": rng[1].isoformat(),
                                "daily_totals": list(totals.values()), "meals": meals})

    @mcp.tool(annotations=READ_ONLY)
    def get_supplements() -> Result:
        """Lapo's supplements (dose, timing, active) and which were taken today."""
        day = today()
        with ro() as conn:
            return reply(conn, {"date": day.isoformat(), "supplements": _supplements(conn, day)})

    @mcp.tool(annotations=READ_ONLY)
    def get_week_summary(week_start: str | None = None) -> Result:
        """The weekly numbers (load, hours, done/missed, weight, sleep, HRV) and the saved review for a week."""
        day = today()
        monday = parse_day(week_start, day - timedelta(days=day.weekday()), "week_start")
        if isinstance(monday, str):
            return error(monday)
        with ro() as conn:
            if week_start:
                row = fetch_one(conn, "SELECT * FROM weekly_summary WHERE week_start = ?", (monday.isoformat(),))
            else:
                row = fetch_one(conn, "SELECT * FROM weekly_summary WHERE week_start <= ?"
                                      " ORDER BY week_start DESC LIMIT 1", (monday.isoformat(),))
            key = row["week_start"] if row else monday.isoformat()
            review = fetch_one(conn, "SELECT text, source, saved_at FROM weekly_review WHERE week_start = ?", (key,))
            return reply(conn, {
                "week_start": key,
                "summary": _loads(row["metrics_json"]) if row else None,
                "computed_at": row["computed_at"] if row else None,
                "review": review,
                "note": None if row else "no weekly summary computed for this week yet",
            })
