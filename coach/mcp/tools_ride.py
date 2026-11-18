"""coach-mcp ride tools (U6): Garmin sync on demand, thresholds, ride detail and analysis.

Read tools open the DB read-only and wrap results with ``envelope()``. Each default response
stays well under ~6k characters: laps are capped, streams are downsampled to at most
``MAX_POINTS`` buckets with a ``next_start_s`` cursor. ``sync_now`` is the one write tool.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from coach.analysis import ride
from coach.ingest import garmin_sync
from coach.ingest.garmin_export import POWER_BEST_DURATIONS
from coach.integrations.garmin import GarminError
from coach.integrations.intervals import IntervalsError
from coach.jobs import sync as intervals_job
from coach.jobs.morning import local_now
from coach.mcp.format import (
    Clock,
    Result,
    connect_ro,
    envelope,
    error,
    parse_day,
    utc_now,
    written,
)
from coach.store import thresholds
from coach.store.db import Row, connect_write, fetch_all, fetch_one, today_local
from coach.store.sync import get_state, set_state

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
SYNC = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

IdKind = Literal["internal", "garmin", "icu"]
ID_COLUMNS = {"internal": "id", "garmin": "garmin_activity_id", "icu": "icu_activity_id"}
STREAM_FIELDS = ("power", "hr", "cadence", "speed_mps", "distance_m", "altitude_m", "lat", "lon")
_INT_FIELDS = {"power", "hr", "cadence"}
_FIELD_DIGITS = {"speed_mps": 2, "distance_m": 1, "altitude_m": 1, "lat": 6, "lon": 6}
MAX_POINTS = 300
MAX_LAPS = 30
SYNC_KEY = "mcp_sync_now_last"
SYNC_MIN_INTERVAL = timedelta(minutes=2)
SYNC_DEADLINE_S = 45
SYNC_DAYS = 2
FTP_WINDOW_DAYS = 90
FTP_FACTOR = 0.95
_DAY = "substr(coalesce(a.start_local, a.start_utc), 1, 10)"
_TS = "%Y-%m-%dT%H:%M:%SZ"
_SYNC_ERRORS = (GarminError, IntervalsError, httpx.HTTPError, sqlite3.Error)

Factory = Callable[[], Any]
Today = Callable[[], date]


def _activity(conn: sqlite3.Connection, activity_id: int | str, id_kind: str) -> Row | str:
    """The activity row, or an error message."""
    column = ID_COLUMNS.get(id_kind)
    if column is None:
        return f"id_kind must be one of {sorted(ID_COLUMNS)}, got {id_kind!r}"
    value: int | str = str(activity_id)
    if id_kind != "icu":
        try:
            value = int(activity_id)
        except (TypeError, ValueError):
            return f"{id_kind} activity id must be an integer, got {activity_id!r}"
    row = fetch_one(conn, f"SELECT * FROM activity WHERE {column} = ?", (value,))
    return row if row else f"no activity with {id_kind} id {activity_id}"


def _stream(conn: sqlite3.Connection, act_id: int) -> list[Row]:
    return fetch_all(conn, "SELECT t, power, hr, speed_mps, distance_m, altitude_m FROM activity_stream"
                     " WHERE activity_id = ? ORDER BY t", (act_id,))


def _laps(conn: sqlite3.Connection, act_id: int) -> list[Row]:
    return fetch_all(conn, "SELECT idx, start_s, duration_s, distance_m, avg_power, norm_power, max_power,"
                     " avg_hr, max_hr, avg_cadence, ascent_m, descent_m, trigger FROM activity_lap"
                     " WHERE activity_id = ? ORDER BY idx", (act_id,))


def _zones(conn: sqlite3.Connection, act_id: int) -> list[Row]:
    return fetch_all(conn, "SELECT kind, zone, seconds, low, high FROM activity_zone_time"
                     " WHERE activity_id = ? ORDER BY kind, zone", (act_id,))


def _day_of(act: Row) -> str:
    return str(act.get("start_local") or act["start_utc"])[:10]


def _weight(conn: sqlite3.Connection, act: Row) -> float | None:
    """Manual weight effective on the ride date, else the FIT value, else the athlete profile."""
    manual = thresholds.effective(conn, _day_of(act)).get("weight_kg")
    if manual:
        return float(manual["value"])
    if act.get("weight_kg_used"):
        return float(act["weight_kg_used"])
    row = fetch_one(conn, "SELECT weight_kg FROM athlete ORDER BY id LIMIT 1")
    return row["weight_kg"] if row else None


def _ftp(conn: sqlite3.Connection, act: Row) -> tuple[float | None, str | None]:
    """FTP effective on the ride date: manual, else the FIT value, else the athlete profile."""
    manual = thresholds.effective(conn, _day_of(act)).get("ftp_w")
    if manual:
        return float(manual["value"]), "manual"
    if act.get("ftp_used"):
        return float(act["ftp_used"]), "garmin_fit"
    row = fetch_one(conn, "SELECT ftp_w FROM athlete ORDER BY id LIMIT 1")
    if row and row["ftp_w"]:
        return float(row["ftp_w"]), "athlete_profile"
    return None, None


def _r(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _compact(row: Row) -> Row:
    return {k: v for k, v in row.items() if v is not None}


def activity_detail(conn: sqlite3.Connection, act: Row) -> Result:
    stream = _stream(conn, act["id"])
    laps = _laps(conn, act["id"])
    zone_time: dict[str, list[Row]] = {}
    for z in _zones(conn, act["id"]):
        zone_time.setdefault(z["kind"], []).append(
            {"zone": z["zone"], "seconds": z["seconds"], "low": z["low"], "high": z["high"]})
    summary = {k: act[k] for k in (
        "id", "garmin_activity_id", "icu_activity_id", "source", "sport", "indoor", "name", "start_local",
        "duration_s", "moving_s", "elapsed_s", "distance_m", "elevation_gain_m", "avg_speed_mps",
        "avg_power", "max_power", "avg_hr", "max_hr", "avg_cadence", "calories")}
    # Garmin's avg power skips 0 W seconds; the true average is computed.avg_power
    summary["avg_power_garmin_nonzero"] = summary.pop("avg_power")
    out: Result = {
        "activity": _compact(summary),
        "garmin": _compact({k: act[k] for k in (
            "norm_power", "intensity_factor", "tss", "training_load", "aerobic_te", "anaerobic_te",
            "max_20min_power", "work_kj")}),
        "thresholds": _compact({k: act[k] for k in ("ftp_used", "lthr_used", "max_hr_used", "weight_kg_used")}),
        "computed": None,
        "stream_points": len(stream),
        "laps": [_compact(lap) for lap in laps[:MAX_LAPS]],
        "laps_total": len(laps),
        "zone_time": zone_time,
    }
    ftp, ftp_source = _ftp(conn, act)
    if stream:
        np_w = ride.np(stream)
        if_, tss = ride.intensity(np_w, ftp, ride.moving_s(stream))
        drift_pct = ride.drift(stream)
        out["computed"] = {
            "ftp_w": ftp, "ftp_source": ftp_source,
            "np": _r(np_w), "np_source": "stream", "if": _r(if_, 2), "tss": _r(tss),
            "work_kj": _r(ride.work_kj(stream)),
            **ride.power_summary(stream),
            "drift_pct": _r(drift_pct),
            "drift_note": ride.DRIFT_SHORT_NOTE if drift_pct is None else None,
            "power_zone_time": ([{k: z[k] for k in ("zone", "low", "high", "seconds")}
                                 for z in ride.zone_time(stream, ride.coggan_zones(ftp))] if ftp else None),
        }
    else:
        out["note"] = "no FIT stream stored yet: stream metrics, laps and segments need it (try sync_now)"
        np_w = act.get("norm_power")
        if np_w is not None and ftp:
            if_, tss = ride.intensity(np_w, ftp, act.get("duration_s"))
            out["computed"] = {"ftp_w": ftp, "ftp_source": ftp_source, "np": np_w, "np_source": "garmin",
                               "if": _r(if_, 2), "tss": _r(tss)}
    return out


def _latest(conn: sqlite3.Connection, column: str, day: str) -> Row | None:
    return fetch_one(conn, f"SELECT a.id, {_DAY} AS day, a.{column} AS value FROM activity a"
                     f" WHERE a.{column} IS NOT NULL AND {_DAY} <= ?"
                     " ORDER BY a.start_utc DESC LIMIT 1", (day,))


def athlete_settings(conn: sqlite3.Connection, day: date) -> Result:
    profile = fetch_one(conn, "SELECT ftp_w, weight_kg FROM athlete ORDER BY id LIMIT 1") or {}
    fallback = {"ftp_w": profile.get("ftp_w"), "weight_kg": profile.get("weight_kg")}
    settings: Result = {}
    for name, column, profile_key in (("ftp_w", "ftp_used", "ftp_w"), ("lthr", "lthr_used", None),
                                      ("max_hr", "max_hr_used", None), ("weight_kg", "weight_kg_used", "weight_kg")):
        hit = _latest(conn, column, day.isoformat())
        if hit:
            settings[name] = {"value": hit["value"], "source": "garmin_fit",
                              "activity_id": hit["id"], "date": hit["day"]}
        elif profile_key and fallback[profile_key] is not None:
            settings[name] = {"value": fallback[profile_key], "source": "athlete_profile"}
        else:
            settings[name] = {"value": None, "source": None}
    for name, row in thresholds.effective(conn, day).items():
        garmin = settings[name]["value"] if settings[name]["source"] == "garmin_fit" else None
        settings[name] = {"value": row["value"], "source": "manual", "effective_from": row["effective_from"],
                          "note": row["note"], "garmin_value": garmin}
    since = day - timedelta(days=FTP_WINDOW_DAYS - 1)
    (best,) = conn.execute(
        f"SELECT max(b.watts) FROM activity_power_best b JOIN activity a ON a.id = b.activity_id"
        f" WHERE b.duration_s = 1200 AND {_DAY} BETWEEN ? AND ?", (since.isoformat(), day.isoformat()),
    ).fetchone()
    if best is None:
        (best,) = conn.execute(f"SELECT max(a.max_20min_power) FROM activity a WHERE {_DAY} BETWEEN ? AND ?",
                               (since.isoformat(), day.isoformat())).fetchone()
    ftp = settings["ftp_w"]["value"]
    out: Result = {"date": day.isoformat(), "settings": settings,
                   "best_20min_w_90d": _r(best), "ftp_hint": None}
    if best is not None and ftp and best * FTP_FACTOR > ftp:
        out["ftp_hint"] = (f"best 20-min power in the last {FTP_WINDOW_DAYS} days is {best:.0f} W;"
                           f" x{FTP_FACTOR} = {best * FTP_FACTOR:.0f} W, above FTP {ftp:.0f} W: FTP may be stale")
    return out


def power_curve(conn: sqlite3.Connection, start: date, end: date, durations: list[int]) -> Result:
    rows = fetch_all(conn, f"SELECT b.duration_s, max(b.watts) AS watts, a.id AS activity_id, {_DAY} AS date"
                     " FROM activity_power_best b JOIN activity a ON a.id = b.activity_id"
                     f" WHERE {_DAY} BETWEEN ? AND ? GROUP BY b.duration_s", (start.isoformat(), end.isoformat()))
    by_duration = {r["duration_s"]: r for r in rows}
    curve = []
    for d in durations:
        hit = by_duration.get(d)
        curve.append({"duration_s": d, "watts": _r(hit["watts"]) if hit else None,
                      "activity_id": hit["activity_id"] if hit else None, "date": hit["date"] if hit else None})
    return {"start": start.isoformat(), "end": end.isoformat(), "curve": curve}


def _round_field(name: str, value: float | None) -> float | int | None:
    if value is None:
        return None
    if name in _INT_FIELDS:
        return round(value)
    return round(value, _FIELD_DIGITS.get(name, 1))


def streams(conn: sqlite3.Connection, act_id: int, fields: list[str], resolution_s: int,
            start_s: float | None, end_s: float | None) -> Result:
    lo = start_s if start_s is not None else float("-inf")
    hi = end_s if end_s is not None else float("inf")
    cols = ", ".join(f"avg({f})" for f in fields)
    rows = conn.execute(
        f"SELECT CAST(t / ? AS INTEGER) * ? AS bucket, {cols} FROM activity_stream"
        " WHERE activity_id = ? AND t >= ? AND t < ? GROUP BY bucket ORDER BY bucket LIMIT ?",
        (resolution_s, resolution_s, act_id, lo, hi, MAX_POINTS + 1),
    ).fetchall()
    truncated = len(rows) > MAX_POINTS
    next_start = rows[MAX_POINTS][0] if truncated else None
    points = [[r[0], *(_round_field(f, v) for f, v in zip(fields, r[1:], strict=True))] for r in rows[:MAX_POINTS]]
    return {"activity_id": act_id, "resolution_s": resolution_s, "columns": ["t", *fields],
            "points": points, "truncated": truncated, "next_start_s": next_start}


def register(mcp: FastMCP, db_path: Path | str, *, garmin_factory: Factory | None = None,
             intervals_factory: Factory | None = None, fit_dir: Path | str | None = None,
             today: Today = today_local, now: Clock = utc_now) -> None:
    """Add the ride tools. Factories build API clients lazily; None means not configured."""

    @contextmanager
    def ro() -> Iterator[sqlite3.Connection]:
        conn = connect_ro(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def reply(conn: sqlite3.Connection, data: Result | str) -> Result:
        if isinstance(data, str):
            return error(data)
        return data if "error" in data else envelope(conn, data, now())

    def close(client: Any) -> None:
        closer = getattr(client, "close", None)
        if closer is not None:
            closer()

    def garmin_sync_run(conn: sqlite3.Connection, current: datetime) -> Result | str:
        """The sync result, or why Garmin could not be used."""
        if garmin_factory is None or fit_dir is None:
            return "Garmin is not configured"
        try:
            client = garmin_factory()
        except (GarminError, httpx.HTTPError) as exc:
            return f"Garmin unavailable ({exc})"
        day = local_now(current).date()
        try:
            counts = garmin_sync.sync_activities(client, conn, fit_dir, day - timedelta(days=SYNC_DAYS - 1), day,
                                                 deadline=time.monotonic() + SYNC_DEADLINE_S)
        except (GarminError, httpx.HTTPError) as exc:
            return f"Garmin sync failed ({exc})"
        finally:
            close(client)
        line = f"Garmin: {counts['new']} new, {counts['updated']} updated"
        if counts["fit_pending"]:
            line += f"; {counts['fit_pending']} ride(s) still waiting for the FIT file, call sync_now again in a few minutes"
        return written(line, source="garmin", **counts)

    def intervals_sync_run(conn: sqlite3.Connection, current: datetime, reason: str) -> Result:
        if intervals_factory is None:
            return error(f"{reason}; intervals.icu is not configured either")
        try:
            client = intervals_factory()
        except _SYNC_ERRORS as exc:
            return error(f"{reason}; intervals.icu unavailable ({exc})")
        try:
            res = intervals_job.run(conn, client, current)
        except _SYNC_ERRORS as exc:
            return error(f"{reason}; intervals.icu sync failed ({exc})")
        finally:
            close(client)
        return written(f"{reason}, used intervals.icu instead: {res.get('activities', 0)} activities synced",
                       source="intervals", fallback_reason=reason, activities=res.get("activities", 0),
                       wellness=res.get("wellness", 0), new=None, updated=None, fit_pending=0)

    @mcp.tool(annotations=SYNC)
    def sync_now() -> Result:
        """Pull the last 2 days of rides from Garmin now (laps, streams, power bests).

        Use after Lapo finishes a ride. Runs at most once every 2 minutes; falls back to
        intervals.icu when Garmin is not available and says which source it used.
        """
        current = now()
        conn = connect_write(db_path)
        try:
            last = get_state(conn, SYNC_KEY)
            if last:
                try:
                    ago = current - datetime.strptime(last, _TS).replace(tzinfo=current.tzinfo)
                except ValueError:
                    ago = SYNC_MIN_INTERVAL
                if ago < SYNC_MIN_INTERVAL:
                    wait = int((SYNC_MIN_INTERVAL - ago).total_seconds()) + 1
                    return error(f"sync_now ran {int(ago.total_seconds())} s ago; try again in {wait} s")
            set_state(conn, SYNC_KEY, current.strftime(_TS))
            try:
                result = garmin_sync_run(conn, current)
            except garmin_sync.SyncLocked:
                return error("sync already running; try again in a minute")
            if isinstance(result, str):
                return intervals_sync_run(conn, current, result)
            return result
        finally:
            conn.close()

    @mcp.tool(annotations=READ_ONLY)
    def get_athlete_settings(date: str | None = None) -> Result:
        """FTP, LTHR, max HR and weight in effect on a date (default today), each with its source.

        Values come from the latest ride's FIT file on or before the date, else the athlete
        profile. ftp_hint flags an FTP that looks stale against the last 90 days of power.
        """
        day = parse_day(date, today(), "date")
        if isinstance(day, str):
            return error(day)
        with ro() as conn:
            return reply(conn, athlete_settings(conn, day))

    @mcp.tool(annotations=READ_ONLY)
    def get_activity(id: int | str, id_kind: IdKind = "internal") -> Result:
        """One ride in detail: summary, Garmin metrics (NP, IF, TSS), computed metrics from the
        stream, thresholds used, laps and time in zones.

        computed.avg_power is the true average (coasting counts as 0 W over moving time);
        avg_power_nonzero and activity.avg_power_garmin_nonzero skip 0 W seconds. drift_pct is
        Pa:HR decoupling over pedalling seconds only (null under 10 min of pedalling).

        id_kind: 'internal' (coach id, default), 'garmin' (Garmin activity id) or 'icu'.
        """
        with ro() as conn:
            act = _activity(conn, id, id_kind)
            return reply(conn, act if isinstance(act, str) else activity_detail(conn, act))

    @mcp.tool(annotations=READ_ONLY)
    def analyze_segment(id: int | str, by: str, from_: float, to: float, above_w: float | None = None,
                        above_hr: float | None = None, id_kind: IdKind = "internal") -> Result:
        """Stats for part of a ride: duration, distance, gain, avg/NP power (avg counts coasting
        as 0 W; avg_power_nonzero and coasting_pct too), W/kg, VAM, HR, zone time, seconds and %
        strictly above `above_w` (W) and `above_hr` (bpm), drift over pedalling seconds (null
        with drift_note under 10 min of pedalling).

        by='time' (seconds from start, [from_, to)), 'distance' (metres), 'altitude' (first
        crossing of from_ m, then of to m: a climb) or 'lap' (0-based lap indexes, inclusive).
        """
        with ro() as conn:
            act = _activity(conn, id, id_kind)
            if isinstance(act, str):
                return error(act)
            stream = _stream(conn, act["id"])
            if not stream:
                return error("no FIT stream stored for this ride yet; try sync_now")
            ftp, ftp_source = _ftp(conn, act)
            # HR zones as Garmin stored them; power zones are Coggan on the effective FTP
            zones: list[Any] = [z for z in _zones(conn, act["id"]) if z["kind"] == "hr"]
            if ftp:
                zones += ride.coggan_zones(ftp)
            try:
                seg = ride.segment(stream, by, from_, to, above_w=above_w, above_hr=above_hr, zones=zones,
                                   weight_kg=_weight(conn, act), laps=_laps(conn, act["id"]))
            except ValueError as exc:
                return error(str(exc))
            for key, metric in (("pct_ftp_avg", "avg_power"), ("pct_ftp_np", "np")):
                seg[key] = _r(100 * seg[metric] / ftp) if ftp and seg[metric] is not None else None
            return reply(conn, {"activity_id": act["id"], "ftp_w": ftp, "ftp_source": ftp_source, "segment": seg})

    @mcp.tool(annotations=READ_ONLY)
    def get_power_curve(start: str | None = None, end: str | None = None,
                        durations: list[int] | None = None) -> Result:
        """Best power per duration over a date range (default the last 90 days), with the ride
        and date of each best. durations: seconds, from 5, 15, 30, 60, 120, 300, 600, 1200,
        1800, 3600, 5400, 7200 (default all).
        """
        end_day = parse_day(end, today(), "end")
        if isinstance(end_day, str):
            return error(end_day)
        start_day = parse_day(start, end_day - timedelta(days=FTP_WINDOW_DAYS - 1), "start")
        if isinstance(start_day, str):
            return error(start_day)
        if start_day > end_day:
            return error("start must be on or before end")
        wanted = list(POWER_BEST_DURATIONS) if not durations else durations
        bad = [d for d in wanted if d not in POWER_BEST_DURATIONS]
        if bad:
            return error(f"unsupported durations {bad}; use any of {list(POWER_BEST_DURATIONS)}")
        with ro() as conn:
            return reply(conn, power_curve(conn, start_day, end_day, sorted(set(wanted))))

    @mcp.tool(annotations=READ_ONLY)
    def get_streams(id: int | str, fields: list[str], resolution_s: int = 10, start_s: float | None = None,
                    end_s: float | None = None, id_kind: IdKind = "internal") -> Result:
        """Ride samples averaged into `resolution_s` buckets, at most 300 points per call.

        fields: any of power, hr, cadence, speed_mps, distance_m, altitude_m, lat, lon.
        Window with start_s/end_s (seconds from start). When truncated, call again with
        start_s = next_start_s.
        """
        if not fields:
            return error(f"fields must list at least one of {list(STREAM_FIELDS)}")
        bad = [f for f in fields if f not in STREAM_FIELDS]
        if bad:
            return error(f"unknown fields {bad}; use any of {list(STREAM_FIELDS)}")
        if resolution_s < 1:
            return error("resolution_s must be at least 1")
        with ro() as conn:
            act = _activity(conn, id, id_kind)
            if isinstance(act, str):
                return error(act)
            return reply(conn, streams(conn, act["id"], list(dict.fromkeys(fields)), resolution_s, start_s, end_s))
