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

Backfill:  python -m coach.ingest.garmin_sync --since 2026-11-01 [--until YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from coach.integrations.garmin import GarminClient

WELLNESS_KEY = "garmin_wellness_last_ok"
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


def main(argv: list[str] | None = None) -> None:
    from coach.config import Settings
    from coach.store.db import connect_write

    parser = argparse.ArgumentParser(prog="garmin-sync", description="Backfill Garmin wellness into wellness_day")
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--until", type=date.fromisoformat, default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--db", default=None, help="override COACH_DB_PATH")
    args = parser.parse_args(argv)
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    client = GarminClient.from_settings(settings)
    conn = connect_write(args.db or settings.coach_db_path)
    try:
        n = sync_wellness(client, conn, args.since, args.until or date.today())
    finally:
        conn.close()
        client.close()
    print(f"garmin wellness: {n} days written")


if __name__ == "__main__":
    main()
