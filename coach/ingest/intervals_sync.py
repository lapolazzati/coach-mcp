"""intervals.icu ingestion: wellness_day and activity, kept fresh from the API.

Writes only raw tables (wellness_day, weight, activity, sync_state) plus the
readiness sleep_seen_at marker through coach.store.derived.

Rules:
- A null/absent intervals.icu field never overwrites a stored value (COALESCE).
- Activity matching, in order: (1) icu_activity_id; (2) `external_id` when it is
  numeric (intervals.icu stores the Garmin activityId there for Garmin-synced
  rides) equal to garmin_activity_id; (3) same sport_group with start_utc within
  MATCH_TOLERANCE_S. A match gets icu_activity_id attached; no duplicate row.
- sync_state.<key> is written only after a successful run; failures re-raise
  and leave the previous value.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

from coach.integrations.intervals import IntervalsClient
from coach.store import derived

WELLNESS_KEY = "intervals_wellness_last_ok"
ACTIVITIES_KEY = "intervals_activities_last_ok"
SOURCE = "intervals"
MATCH_TOLERANCE_S = 300

# wellness_day column -> candidate intervals.icu keys (first present wins)
WELLNESS_FIELDS: dict[str, tuple[str, ...]] = {
    "sleep_secs": ("sleepSecs",),
    "sleep_score": ("sleepScore",),
    "hrv_ms": ("hrv",),
    "resting_hr": ("restingHR",),
    "ctl": ("ctl",),
    "atl": ("atl",),
    "body_battery_high": ("bodyBatteryHigh", "bodyBattery"),
    "body_battery_low": ("bodyBatteryLow",),
    "steps": ("steps",),
}
INT_COLUMNS = {"sleep_secs", "sleep_score", "resting_hr", "body_battery_high", "body_battery_low", "steps"}

CYCLING = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide", "Velomobile", "Handcycle"}
RUNNING = {"Run", "TrailRun", "VirtualRun"}
SWIMMING = {"Swim", "OpenWaterSwim"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_ok(conn: sqlite3.Connection, key: str) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, now, now),
    )
    conn.commit()


def _pick(raw: dict, keys: tuple[str, ...], integer: bool):
    for k in keys:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(v) if integer else float(v)
    return None


def parse_wellness(raw: dict) -> dict:
    return {col: _pick(raw, keys, col in INT_COLUMNS) for col, keys in WELLNESS_FIELDS.items()}


# --- wellness -----------------------------------------------------------------


def sync_wellness(client: IntervalsClient, conn: sqlite3.Connection, oldest: date, newest: date) -> int:
    """Upsert wellness rows for oldest..newest; returns the number of days written."""
    payload = client.get_wellness(oldest, newest)
    now = _now()
    newly_slept: list[str] = []
    written = 0
    for raw in payload:
        day = str(raw.get("id") or "")[:10]
        if len(day) != 10:
            continue
        values = parse_wellness(raw)
        cols = list(values)
        before = conn.execute("SELECT sleep_secs FROM wellness_day WHERE date = ?", (day,)).fetchone()
        sql = (
            f"INSERT INTO wellness_day (date, {', '.join(cols)}, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in cols)}, ?) "
            "ON CONFLICT (date) DO UPDATE SET "
            + ", ".join(f"{c} = COALESCE(excluded.{c}, wellness_day.{c})" for c in cols)
            + ", updated_at = excluded.updated_at"
        )
        conn.execute(sql, (day, *values.values(), now))
        kg = raw.get("weight")
        if isinstance(kg, (int, float)) and kg > 0:
            conn.execute(
                "INSERT INTO weight (date, kg, source, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (date, source) DO UPDATE SET kg = excluded.kg, updated_at = excluded.updated_at",
                (day, round(float(kg), 2), SOURCE, now),
            )
        if values["sleep_secs"] and (before is None or before[0] is None):
            newly_slept.append(day)
        written += 1
    conn.commit()
    for day in newly_slept:
        derived.mark_sleep_seen(conn, day)
    _record_ok(conn, WELLNESS_KEY)
    return written


# --- activities ---------------------------------------------------------------


def _sport_group(kind: str) -> str:
    if kind in CYCLING:
        return "cycling"
    if kind in RUNNING:
        return "running"
    if kind in SWIMMING:
        return "swimming"
    if kind == "WeightTraining":
        return "strength"
    return "other"


def _utc_iso(value: str | None) -> str | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def parse_activity(raw: dict, now: str) -> dict | None:
    start = _utc_iso(raw.get("start_date"))
    if raw.get("id") is None or start is None:
        return None
    kind = raw.get("type") or "Other"
    return {
        "icu_activity_id": str(raw["id"]),
        "source": SOURCE,
        "sport": kind,
        "sport_group": _sport_group(kind),
        "indoor": int(kind == "VirtualRide" or bool(raw.get("trainer"))),
        "name": raw.get("name"),
        "start_utc": start,
        "start_local": raw.get("start_date_local"),
        "duration_s": raw.get("moving_time") or raw.get("elapsed_time"),
        "moving_s": raw.get("moving_time"),
        "elapsed_s": raw.get("elapsed_time"),
        "distance_m": raw.get("distance"),
        "elevation_gain_m": raw.get("total_elevation_gain"),
        "avg_speed_mps": raw.get("average_speed"),
        "avg_hr": raw.get("average_heartrate"),
        "max_hr": raw.get("max_heartrate"),
        "avg_power": raw.get("icu_average_watts"),
        "norm_power": raw.get("icu_weighted_avg_watts"),
        "avg_cadence": raw.get("average_cadence"),
        "calories": raw.get("calories"),
        "training_load": raw.get("icu_training_load"),
        "raw_json": json.dumps(raw, sort_keys=True),
        "updated_at": now,
    }


def _garmin_id(raw: dict) -> int | None:
    ext = str(raw.get("external_id") or "")
    return int(ext) if ext.isdigit() else None


def _find_match(conn: sqlite3.Connection, row: dict, garmin_id: int | None) -> tuple[int, str] | None:
    hit = conn.execute("SELECT id, source FROM activity WHERE icu_activity_id = ?", (row["icu_activity_id"],)).fetchone()
    if hit:
        return hit[0], hit[1]
    if garmin_id is not None:
        hit = conn.execute("SELECT id, source FROM activity WHERE garmin_activity_id = ?", (garmin_id,)).fetchone()
        if hit:
            return hit[0], hit[1]
    t = _epoch(row["start_utc"])
    candidates = conn.execute(
        "SELECT id, source, start_utc FROM activity WHERE sport_group = ? AND icu_activity_id IS NULL "
        "AND start_utc BETWEEN ? AND ? ORDER BY start_utc",
        (
            row["sport_group"],
            datetime.fromtimestamp(t - MATCH_TOLERANCE_S, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            datetime.fromtimestamp(t + MATCH_TOLERANCE_S, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    ).fetchall()
    if not candidates:
        return None
    best = min(candidates, key=lambda c: abs(_epoch(c[2]) - t))
    return best[0], best[1]


METRIC_COLUMNS = (
    "name", "start_local", "duration_s", "moving_s", "elapsed_s", "distance_m", "elevation_gain_m",
    "avg_speed_mps", "avg_hr", "max_hr", "avg_power", "norm_power", "avg_cadence", "calories", "training_load",
)


def sync_activities(client: IntervalsClient, conn: sqlite3.Connection, oldest: date, newest: date) -> int:
    """Upsert activities by icu id, linking to Garmin-imported rows; returns activities processed."""
    payload = client.list_activities(oldest, newest)
    now = _now()
    n = 0
    for raw in payload:
        row = parse_activity(raw, now)
        if row is None:
            continue
        garmin_id = _garmin_id(raw)
        match = _find_match(conn, row, garmin_id)
        if match is None:
            cols = [*row, "garmin_activity_id"]
            conn.execute(
                f"INSERT INTO activity ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (*row.values(), garmin_id),
            )
        else:
            act_id, source = match
            # Garmin-sourced rows keep their own values; intervals only fills gaps. Own rows are refreshed.
            if source == SOURCE:
                sets = [f"{c} = COALESCE(?, {c})" for c in METRIC_COLUMNS]
            else:
                sets = [f"{c} = COALESCE({c}, ?)" for c in METRIC_COLUMNS]
            conn.execute(
                f"UPDATE activity SET icu_activity_id = ?, garmin_activity_id = COALESCE(garmin_activity_id, ?), "
                f"{', '.join(sets)}, updated_at = ? WHERE id = ?",
                (row["icu_activity_id"], garmin_id, *(row[c] for c in METRIC_COLUMNS), now, act_id),
            )
        n += 1
    conn.commit()
    _record_ok(conn, ACTIVITIES_KEY)
    return n
