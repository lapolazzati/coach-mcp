"""Garmin Connect wellness ingestion: the Garmin-only wellness_day columns.

Fills what intervals.icu does not carry: sleep stages and window, sleep stress,
daily avg stress, body battery high/low and the HRV weekly average. It never
writes columns intervals.icu owns (sleep_secs, sleep_score, hrv_ms, resting_hr,
steps, ctl, atl). Readiness and acute/chronic load are not in the Personal API
and stay null.

Rules:
- A null/absent Garmin field never overwrites a stored value (COALESCE).
- A present Garmin value replaces an earlier one, so today's partial stress and
  body battery are refreshed on every run.
- sync_state.<WELLNESS_KEY> is written only after a successful run.

Activities (sync_activities): summaries for every sport; cycling rides also get
the original FIT (gzipped under fit_dir) with streams, laps, zone time and the
thresholds the device used. Rows from intervals.icu or the export are matched
(garmin id, else start within 5 min and same sport group) and updated in place.

Backfill:  python -m coach.ingest.garmin_sync --since 2026-11-01 [--until YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import secrets
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from coach.ingest.garmin_export import (
    ACTIVITY_COLUMNS,
    INDOOR,
    parse_fit_ride,
    power_bests,
    sport_group,
)
from coach.integrations.garmin import GarminClient, GarminError

WELLNESS_KEY = "garmin_wellness_last_ok"
log = logging.getLogger(__name__)
ACTIVITIES_KEY = "garmin_activities_last_ok"
LOCK_KEY = "garmin_activities_lock"
LOCK_STALE_S = 1800  # a lock older than this belongs to a dead run
MATCH_TOLERANCE_S = 300  # intervals/export row vs Garmin start
OVERLAP_DAYS = 2  # re-read this many days before the cursor
COLUMNS = (
    "sleep_deep_secs", "sleep_light_secs", "sleep_rem_secs", "sleep_awake_secs",
    "sleep_start_utc", "sleep_end_utc", "sleep_avg_stress",
    "avg_stress", "body_battery_high", "body_battery_low", "hrv_weekly_avg",
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_iso(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _non_negative(value: Any) -> Any:
    # Garmin uses -1/-2 for "not enough data"
    return value if isinstance(value, (int, float)) and value >= 0 else None


def _in_range(day: Any, oldest: date, newest: date) -> str | None:
    if not isinstance(day, str):
        return None
    key = day[:10]
    return key if oldest.isoformat() <= key <= newest.isoformat() else None


def parse(sleep: list[dict], summaries: list[dict], hrv: list[dict], oldest: date, newest: date) -> dict[str, dict]:
    rows: dict[str, dict] = {}

    def put(day: str, **values: Any) -> None:
        rows.setdefault(day, {}).update(values)

    for s in sleep:
        day = _in_range(s.get("calendarDate"), oldest, newest)
        if day:
            put(day,
                sleep_deep_secs=s.get("deepSleepSeconds"),
                sleep_light_secs=s.get("lightSleepSeconds"),
                sleep_rem_secs=s.get("remSleepSeconds"),
                sleep_awake_secs=s.get("awakeSleepSeconds"),
                sleep_start_utc=_ms_to_iso(s.get("sleepStartTimestampGMT")),
                sleep_end_utc=_ms_to_iso(s.get("sleepEndTimestampGMT")),
                sleep_avg_stress=_non_negative(s.get("avgSleepStress")))
    for d in summaries:
        day = _in_range(d.get("calendarDate"), oldest, newest)
        if day:
            stress = d.get("stress") or {}
            battery = d.get("bodyBattery") or {}
            put(day,
                avg_stress=_non_negative(stress.get("avgLevel")),
                body_battery_high=_non_negative(battery.get("maxValue")),
                body_battery_low=_non_negative(battery.get("minValue")))
    for h in hrv:
        day = _in_range(h.get("calendarDate"), oldest, newest)
        if day:
            put(day, hrv_weekly_avg=h.get("weeklyAvg"))
    # drop days with nothing to say, so an empty Garmin day creates no row
    return {day: v for day, v in rows.items() if any(x is not None for x in v.values())}


def _record_ok(conn: sqlite3.Connection, key: str) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, now, now),
    )
    conn.commit()


def sync_wellness(client: GarminClient, conn: sqlite3.Connection, oldest: date, newest: date) -> int:
    rows = parse(client.sleep_summaries(oldest, newest), client.daily_summaries(oldest, newest),
                 client.hrv_summaries(oldest, newest), oldest, newest)
    now = _now()
    sql = (
        f"INSERT INTO wellness_day (date, {', '.join(COLUMNS)}, updated_at) "
        f"VALUES (?, {', '.join('?' for _ in COLUMNS)}, ?) "
        "ON CONFLICT (date) DO UPDATE SET "
        + ", ".join(f"{c} = COALESCE(excluded.{c}, wellness_day.{c})" for c in COLUMNS)
        + ", updated_at = excluded.updated_at"
    )
    for day in sorted(rows):
        conn.execute(sql, (day, *(rows[day].get(c) for c in COLUMNS), now))
    conn.commit()
    _record_ok(conn, WELLNESS_KEY)
    return len(rows)


# --- activities --------------------------------------------------------------

API_COLUMNS = (*ACTIVITY_COLUMNS, "tss", "intensity_factor")
LAP_COLUMNS = ("idx", "start_utc", "start_s", "duration_s", "distance_m", "avg_power", "norm_power",
               "max_power", "avg_hr", "max_hr", "avg_cadence", "ascent_m", "descent_m", "trigger")


class SyncLocked(RuntimeError):
    """Another activity sync holds the lock."""


def _gmt(value: Any, utc: bool) -> str | None:
    """Garmin 'YYYY-MM-DD HH:MM:SS' -> ISO (with Z when UTC)."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    iso = value[:19].replace(" ", "T")
    return iso + "Z" if utc else iso


def parse_api_activity(raw: dict, now: str) -> dict[str, Any]:
    """One activity-list summary -> activity columns (source garmin_api)."""
    sport = (raw.get("activityType") or {}).get("typeKey") or "other"
    clean = {k: v for k, v in raw.items() if "owner" not in k.lower()}
    return {
        "garmin_activity_id": raw["activityId"],
        "source": "garmin_api",
        "sport": sport,
        "sport_group": sport_group(sport),
        "indoor": int(sport in INDOOR),
        "name": raw.get("activityName"),
        "start_utc": _gmt(raw.get("startTimeGMT"), utc=True),
        "start_local": _gmt(raw.get("startTimeLocal"), utc=False),
        "duration_s": raw.get("duration"),
        "moving_s": raw.get("movingDuration"),
        "elapsed_s": raw.get("elapsedDuration"),
        "distance_m": raw.get("distance"),
        "elevation_gain_m": raw.get("elevationGain"),
        "avg_speed_mps": raw.get("averageSpeed"),
        "avg_hr": raw.get("averageHR"),
        "max_hr": raw.get("maxHR"),
        "avg_power": raw.get("avgPower"),
        "max_power": raw.get("maxPower"),
        "norm_power": raw.get("normPower"),
        "max_20min_power": raw.get("max20MinPower"),
        "avg_cadence": raw.get("averageBikingCadenceInRevPerMinute") or raw.get("averageRunningCadenceInStepsPerMinute"),
        "calories": raw.get("calories"),
        "training_load": raw.get("activityTrainingLoad"),
        "aerobic_te": raw.get("aerobicTrainingEffect"),
        "anaerobic_te": raw.get("anaerobicTrainingEffect"),
        "raw_json": json.dumps(clean, sort_keys=True),
        "updated_at": now,
        "tss": raw.get("trainingStressScore"),
        "intensity_factor": raw.get("intensityFactor"),
    }


def _acquire_lock(conn: sqlite3.Connection) -> str:
    token = secrets.token_hex(8)
    now = datetime.now(UTC)
    stale = (now - timedelta(seconds=LOCK_STALE_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
        "WHERE sync_state.updated_at < ?",
        (LOCK_KEY, token, now.strftime("%Y-%m-%dT%H:%M:%SZ"), stale),
    )
    conn.commit()
    held = conn.execute("SELECT value FROM sync_state WHERE key = ?", (LOCK_KEY,)).fetchone()
    if not held or held[0] != token:
        raise SyncLocked("another Garmin activity sync is running")
    return token


def _release_lock(conn: sqlite3.Connection, token: str) -> None:
    conn.rollback()
    conn.execute("DELETE FROM sync_state WHERE key = ? AND value = ?", (LOCK_KEY, token))
    conn.commit()


def _match(conn: sqlite3.Connection, row: dict) -> tuple[int, str | None] | None:
    """(id, fit_path) of the row this activity already is, if any (KD6)."""
    hit = conn.execute("SELECT id, fit_path FROM activity WHERE garmin_activity_id = ?",
                       (row["garmin_activity_id"],)).fetchone()
    if hit or row["start_utc"] is None:
        return hit
    return conn.execute(
        "SELECT id, fit_path FROM activity WHERE garmin_activity_id IS NULL AND sport_group = ? "
        "AND abs(julianday(start_utc) - julianday(?)) * 86400 <= ? "
        "ORDER BY abs(julianday(start_utc) - julianday(?)) LIMIT 1",
        (row["sport_group"], row["start_utc"], MATCH_TOLERANCE_S, row["start_utc"]),
    ).fetchone()


def _upsert_summary(conn: sqlite3.Connection, row: dict, existing: int | None) -> int:
    if existing is None:
        cols = ", ".join(API_COLUMNS)
        marks = ", ".join("?" for _ in API_COLUMNS)
        cur = conn.execute(f"INSERT INTO activity ({cols}) VALUES ({marks})", [row[c] for c in API_COLUMNS])
        return cur.lastrowid
    sets = ", ".join(f"{c} = ?" for c in API_COLUMNS)
    conn.execute(f"UPDATE activity SET {sets} WHERE id = ?", [*(row[c] for c in API_COLUMNS), existing])
    return existing


def _store_fit(conn: sqlite3.Connection, act_id: int, gid: int, data: bytes, fit_dir: Path) -> None:
    fit_dir.mkdir(parents=True, exist_ok=True)
    name = f"{gid}.fit.gz"
    (fit_dir / name).write_bytes(gzip.compress(data))
    ride = parse_fit_ride(data)
    if ride is not None:
        th, ses = ride.thresholds, ride.session
        conn.execute(
            "UPDATE activity SET work_kj = ?, ftp_used = ?, lthr_used = ?, max_hr_used = ?, weight_kg_used = ?,"
            " tss = COALESCE(tss, ?), intensity_factor = COALESCE(intensity_factor, ?) WHERE id = ?",
            (ses.get("work_kj"), th.get("ftp"), th.get("lthr"), th.get("max_hr"), th.get("weight_kg"),
             ses.get("tss"), ses.get("intensity_factor"), act_id),
        )
        for table in ("activity_stream", "activity_power_best", "activity_lap", "activity_zone_time"):
            conn.execute(f"DELETE FROM {table} WHERE activity_id = ?", (act_id,))
        conn.executemany(
            "INSERT INTO activity_stream (activity_id, t, power, hr, cadence, speed_mps, distance_m,"
            " altitude_m, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(act_id, *r) for r in ride.records],
        )
        conn.executemany(
            "INSERT INTO activity_power_best (activity_id, duration_s, watts) VALUES (?, ?, ?)",
            [(act_id, d, w) for d, w in power_bests(ride.records).items()],
        )
        start = datetime.fromtimestamp(ride.start_epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.executemany(
            f"INSERT INTO activity_lap (activity_id, {', '.join(LAP_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' for _ in LAP_COLUMNS)})",
            [(act_id, *({**lap, "start_utc": lap.get("start_utc") or start}.get(c) for c in LAP_COLUMNS))
             for lap in ride.laps],
        )
        conn.executemany(
            "INSERT INTO activity_zone_time (activity_id, kind, zone, seconds, low, high) VALUES (?, ?, ?, ?, ?, ?)",
            [(act_id, z["kind"], z["zone"], z["seconds"], z["low"], z["high"]) for z in ride.zones],
        )
    conn.execute("UPDATE activity SET fit_path = ? WHERE id = ?", (name, act_id))


def activities_window(conn: sqlite3.Connection, today: date) -> date:
    """Oldest day to re-read: the cursor's day (or today) minus the overlap."""
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (ACTIVITIES_KEY,)).fetchone()
    last = date.fromisoformat(row[0][:10]) if row and row[0] else today
    return min(last, today) - timedelta(days=OVERLAP_DAYS)


def sync_activities(client: GarminClient, conn: sqlite3.Connection, fit_dir: Path | str,
                    oldest: date, newest: date, deadline: float | None = None) -> dict[str, int]:
    """Upsert Garmin activities; FIT detail for rides until the monotonic deadline."""
    fit_dir = Path(fit_dir)
    counts = {"new": 0, "updated": 0, "fit_pending": 0, "errors": 0}
    token = _acquire_lock(conn)
    try:
        for raw in client.activities(oldest, newest):
            if not isinstance(raw, dict) or not raw.get("activityId"):
                continue
            row = parse_api_activity(raw, _now())
            hit = _match(conn, row)
            act_id = _upsert_summary(conn, row, hit[0] if hit else None)
            conn.commit()
            counts["updated" if hit else "new"] += 1
            if row["sport_group"] != "cycling" or (hit and hit[1]):
                continue
            if deadline is not None and time.monotonic() > deadline:
                counts["fit_pending"] += 1
                continue
            try:
                data = client.fit(row["garmin_activity_id"])
            except GarminError:
                counts["errors"] += 1
                counts["fit_pending"] += 1
                continue
            try:
                _store_fit(conn, act_id, row["garmin_activity_id"], data, fit_dir)
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("storing FIT for activity %s failed", row["garmin_activity_id"])
                counts["errors"] += 1
                counts["fit_pending"] += 1
        _record_ok(conn, ACTIVITIES_KEY)
    finally:
        _release_lock(conn, token)
    return counts


def main(argv: list[str] | None = None) -> None:
    from coach.config import Settings
    from coach.store.db import connect_write

    parser = argparse.ArgumentParser(prog="garmin-sync", description="Backfill Garmin wellness and activities")
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--until", type=date.fromisoformat, default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--db", default=None, help="override COACH_DB_PATH")
    parser.add_argument("--fit-dir", default=None, help="override COACH_FIT_DIR")
    parser.add_argument("--no-wellness", action="store_true")
    parser.add_argument("--no-activities", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    client = GarminClient.from_settings(settings)
    conn = connect_write(args.db or settings.coach_db_path)
    newest = args.until or date.today()
    try:
        if not args.no_wellness:
            n = sync_wellness(client, conn, args.since, newest)
            print(f"garmin wellness: {n} days written")
        if not args.no_activities:
            res = sync_activities(client, conn, args.fit_dir or settings.fit_dir, args.since, newest)
            print("garmin activities: " + ", ".join(f"{k} {v}" for k, v in res.items()))
    finally:
        conn.close()
        client.close()


if __name__ == "__main__":
    main()
