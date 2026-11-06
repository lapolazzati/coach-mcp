"""One-shot importer for a Garmin Connect GDPR export (the DI_CONNECT folder).

Reads activity summaries (all years), daily wellness (sleep, HRV, resting HR,
stress, body battery, training readiness, acute/chronic load), weight history,
and FIT streams for cycling rides inside a recent window. Everything is keyed
on Garmin activityId or calendar date, so re-running is safe.

The export is untrusted input: it is only ever parsed as JSON / FIT data.
"""

from __future__ import annotations

import bisect
import io
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

SOURCE = "garmin_export"

CYCLING = {"road_biking", "indoor_cycling", "cycling", "virtual_ride", "mountain_biking", "gravel_cycling"}
INDOOR = {"indoor_cycling", "virtual_ride", "treadmill_running", "indoor_running", "indoor_rowing"}
RUNNING = {"running", "trail_running", "treadmill_running", "track_running", "indoor_running"}
STRENGTH = {"strength_training"}
SWIMMING = {"lap_swimming", "open_water_swimming"}

POWER_BEST_DURATIONS = (5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 5400, 7200)
FIT_MATCH_TOLERANCE_S = 120
SEMICIRCLE_TO_DEG = 180 / 2**31


@dataclass
class ImportStats:
    activities: int = 0
    activities_skipped: int = 0
    wellness_days: dict[str, int] = field(default_factory=dict)
    weights: int = 0
    vo2max: int = 0
    fit_files_scanned: int = 0
    rides_with_streams: int = 0
    rides_streams_already_present: int = 0
    ride_fits_unmatched: int = 0


# --- helpers ---------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_iso(ms: float | None, utc: bool = True) -> str | None:
    if ms is None:
        return None
    dt = datetime.fromtimestamp(ms / 1000, UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ" if utc else "%Y-%m-%dT%H:%M:%S")


def _date(value) -> str | None:
    """Calendar date as YYYY-MM-DD from either a string or a ms epoch at 00:00 UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%d")
    return str(value)[:10]


def _gmt_string_to_iso(value: str | None) -> str | None:
    # "2024-05-01T22:13:00.0" (GMT) -> "2024-05-01T22:13:00Z"
    return f"{value[:19]}Z" if value else None


def _scaled(value, factor: float) -> float | None:
    return None if value is None else value * factor


def _non_negative(value):
    return value if value is not None and value >= 0 else None


def _iso_to_epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def _load_json_files(root: Path, pattern: str) -> Iterator[dict]:
    for path in sorted(root.glob(pattern)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            yield from (row for row in data if isinstance(row, dict))
        elif isinstance(data, dict):
            yield data


def sport_group(sport: str) -> str:
    if sport in CYCLING:
        return "cycling"
    if sport in RUNNING:
        return "running"
    if sport in STRENGTH:
        return "strength"
    if sport in SWIMMING:
        return "swimming"
    return "other"


# --- schema ----------------------------------------------------------------


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    from coach.db.migrate import migrate

    migrate(conn)


# --- activities ------------------------------------------------------------

ACTIVITY_COLUMNS = (
    "garmin_activity_id", "source", "sport", "sport_group", "indoor", "name", "start_utc", "start_local",
    "duration_s", "moving_s", "elapsed_s", "distance_m", "elevation_gain_m", "avg_speed_mps", "avg_hr",
    "max_hr", "avg_power", "max_power", "norm_power", "max_20min_power", "avg_cadence", "calories",
    "training_load", "aerobic_te", "anaerobic_te", "raw_json", "updated_at",
)


def parse_activity(raw: dict, now: str) -> dict | None:
    activity_id = raw.get("activityId")
    start = raw.get("startTimeGmt")
    if activity_id is None or start is None:
        return None
    sport = raw.get("activityType") or "unknown"
    if isinstance(sport, dict):  # older exports nest it
        sport = sport.get("typeKey") or "unknown"
    return {
        "garmin_activity_id": int(activity_id),
        "source": SOURCE,
        "sport": sport,
        "sport_group": sport_group(sport),
        "indoor": int(sport in INDOOR),
        "name": raw.get("name"),
        "start_utc": _ms_to_iso(start),
        "start_local": _ms_to_iso(raw.get("startTimeLocal"), utc=False),
        "duration_s": _scaled(raw.get("duration"), 1 / 1000),
        "moving_s": _scaled(raw.get("movingDuration"), 1 / 1000),
        "elapsed_s": _scaled(raw.get("elapsedDuration"), 1 / 1000),
        "distance_m": _scaled(raw.get("distance"), 1 / 100),
        "elevation_gain_m": _scaled(raw.get("elevationGain"), 1 / 100),
        "avg_speed_mps": _scaled(raw.get("avgSpeed"), 10),
        "avg_hr": raw.get("avgHr"),
        "max_hr": raw.get("maxHr"),
        "avg_power": raw.get("avgPower"),
        "max_power": raw.get("maxPower"),
        "norm_power": raw.get("normPower"),
        "max_20min_power": raw.get("max20MinPower"),
        "avg_cadence": raw.get("avgBikeCadence", raw.get("avgRunCadence")),
        "calories": raw.get("calories"),
        "training_load": raw.get("activityTrainingLoad"),
        "aerobic_te": raw.get("aerobicTrainingEffect"),
        "anaerobic_te": raw.get("anaerobicTrainingEffect"),
        "raw_json": json.dumps(raw, sort_keys=True),
        "updated_at": now,
    }


def import_activities(conn: sqlite3.Connection, export: Path, stats: ImportStats) -> None:
    now = _now()
    cols = ", ".join(ACTIVITY_COLUMNS)
    marks = ", ".join(f":{c}" for c in ACTIVITY_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in ACTIVITY_COLUMNS if c != "garmin_activity_id")
    sql = (
        f"INSERT INTO activity ({cols}) VALUES ({marks}) "
        f"ON CONFLICT (garmin_activity_id) DO UPDATE SET {updates}"
    )
    for wrapper in _load_json_files(export, "DI-Connect-Fitness/*summarizedActivities.json"):
        for raw in wrapper.get("summarizedActivitiesExport", []):
            row = parse_activity(raw, now)
            if row is None:
                stats.activities_skipped += 1
                continue
            conn.execute(sql, row)
            stats.activities += 1


# --- wellness --------------------------------------------------------------


def _upsert_wellness(conn: sqlite3.Connection, rows: dict[str, dict], source_name: str, stats: ImportStats) -> None:
    """Fill only the given columns; a null never overwrites a value already stored."""
    now = _now()
    for date, values in rows.items():
        cols = list(values)
        sql = (
            f"INSERT INTO wellness_day (date, {', '.join(cols)}, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in cols)}, ?) "
            f"ON CONFLICT (date) DO UPDATE SET "
            + ", ".join(f"{c} = COALESCE(excluded.{c}, wellness_day.{c})" for c in cols)
            + ", updated_at = excluded.updated_at"
        )
        conn.execute(sql, (date, *values.values(), now))
    stats.wellness_days[source_name] = len(rows)


def parse_sleep(export: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for raw in _load_json_files(export, "DI-Connect-Wellness/*sleepData.json"):
        date = _date(raw.get("calendarDate"))
        if date is None:  # {"retro": ...} placeholders carry no sleep data
            continue
        stages = [raw.get(k) for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")]
        rows[date] = {
            "sleep_secs": sum(s for s in stages if s) if any(s is not None for s in stages) else None,
            "sleep_score": (raw.get("sleepScores") or {}).get("overallScore"),
            "sleep_deep_secs": raw.get("deepSleepSeconds"),
            "sleep_light_secs": raw.get("lightSleepSeconds"),
            "sleep_rem_secs": raw.get("remSleepSeconds"),
            "sleep_awake_secs": raw.get("awakeSleepSeconds"),
            "sleep_start_utc": _gmt_string_to_iso(raw.get("sleepStartTimestampGMT")),
            "sleep_end_utc": _gmt_string_to_iso(raw.get("sleepEndTimestampGMT")),
            "sleep_avg_stress": raw.get("avgSleepStress"),
        }
    return rows


def parse_daily_summaries(export: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for raw in _load_json_files(export, "DI-Connect-Aggregator/UDSFile_*.json"):
        date = _date(raw.get("calendarDate"))
        if date is None:
            continue
        stress = {
            s.get("type"): s.get("averageStressLevel")
            for s in (raw.get("allDayStress") or {}).get("aggregatorList", [])
        }
        battery = {
            b.get("bodyBatteryStatType"): b.get("statsValue")
            for b in (raw.get("bodyBattery") or {}).get("bodyBatteryStatList", [])
        }
        row = {
            "resting_hr": raw.get("restingHeartRate"),
            "avg_stress": _non_negative(stress.get("TOTAL")),  # Garmin uses -1/-2 for "not enough data"
            "body_battery_high": battery.get("HIGHEST"),
            "body_battery_low": battery.get("LOWEST"),
            "steps": raw.get("totalSteps"),
        }
        # Duplicate dates across files: merge, keeping non-null values.
        merged = rows.setdefault(date, dict.fromkeys(row))
        merged.update({k: v for k, v in row.items() if v is not None})
    return rows


def parse_health_status(export: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for raw in _load_json_files(export, "DI-Connect-Wellness/*healthStatusData.json"):
        date = _date(raw.get("calendarDate"))
        hrv = next((m.get("value") for m in raw.get("metrics", []) if m.get("type") == "HRV"), None)
        if date is not None and hrv is not None:
            rows[date] = {"hrv_ms": hrv}
    return rows


def parse_training_readiness(export: Path) -> dict[str, dict]:
    """One value per day: the post-wake-up reading, else the earliest of the day."""
    by_date: dict[str, list[dict]] = {}
    for raw in _load_json_files(export, "DI-Connect-Metrics/TrainingReadinessDTO_*.json"):
        date = _date(raw.get("calendarDate"))
        if date is not None and raw.get("score") is not None:
            by_date.setdefault(date, []).append(raw)
    rows: dict[str, dict] = {}
    for date, readings in by_date.items():
        readings.sort(key=lambda r: str(r.get("timestamp") or ""))
        pick = next((r for r in readings if r.get("inputContext") == "AFTER_WAKEUP_RESET"), readings[0])
        rows[date] = {
            "readiness_score": pick.get("score"),
            "readiness_level": pick.get("level"),
            "hrv_weekly_avg": pick.get("hrvWeeklyAverage"),
        }
    return rows


def parse_training_load(export: Path) -> dict[str, dict]:
    """Garmin acute/chronic load; the latest reading of each day wins."""
    latest: dict[str, dict] = {}
    for raw in _load_json_files(export, "DI-Connect-Metrics/MetricsAcuteTrainingLoad_*.json"):
        date = _date(raw.get("calendarDate"))
        if date is None:
            continue
        if date not in latest or (raw.get("timestamp") or 0) >= (latest[date].get("timestamp") or 0):
            latest[date] = raw
    rows = {}
    for date, raw in latest.items():
        acute, chronic = raw.get("dailyTrainingLoadAcute"), raw.get("dailyTrainingLoadChronic")
        rows[date] = {
            "acute_load": acute,
            "chronic_load": chronic,
            "acwr": round(acute / chronic, 2) if acute is not None and chronic else None,
        }
    return rows


def import_wellness(conn: sqlite3.Connection, export: Path, stats: ImportStats) -> None:
    _upsert_wellness(conn, parse_sleep(export), "sleep", stats)
    _upsert_wellness(conn, parse_daily_summaries(export), "daily_summary", stats)
    _upsert_wellness(conn, parse_health_status(export), "hrv", stats)
    _upsert_wellness(conn, parse_training_readiness(export), "readiness", stats)
    _upsert_wellness(conn, parse_training_load(export), "training_load", stats)


# --- weight ----------------------------------------------------------------


def parse_weights(export: Path) -> dict[str, float]:
    """kg per date; the highest sequence number of a day wins."""
    best: dict[str, tuple[int, float]] = {}
    for raw in _load_json_files(export, "DI-Connect-Wellness/*userBioMetrics.json"):
        grams = (raw.get("weight") or {}).get("weight")
        meta = raw.get("metaData") or {}
        date = _date(meta.get("calendarDate"))
        if grams is None or date is None:
            continue
        seq = meta.get("sequence") or 0
        if date not in best or seq >= best[date][0]:
            best[date] = (seq, round(grams / 1000, 2))
    return {date: kg for date, (_, kg) in best.items()}


def import_weights(conn: sqlite3.Connection, export: Path, stats: ImportStats) -> None:
    now = _now()
    for date, kg in parse_weights(export).items():
        conn.execute(
            "INSERT INTO weight (date, kg, source, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (date, source) DO UPDATE SET kg = excluded.kg, updated_at = excluded.updated_at",
            (date, kg, SOURCE, now),
        )
        stats.weights += 1


# --- VO2max ----------------------------------------------------------------


def parse_vo2max(export: Path) -> dict[tuple[str, str], dict]:
    """One estimate per (date, sport); when Garmin recorded several, the latest update wins."""
    latest: dict[tuple[str, str], tuple[str, dict]] = {}
    metrics = export / "DI-Connect-Metrics"
    for pattern in ("ActivityVo2Max_*.json", "MetricsMaxMetData_*.json"):
        for raw in _load_json_files(metrics, pattern):
            date, sport, value = _date(raw.get("calendarDate")), raw.get("sport"), raw.get("vo2MaxValue")
            if not date or sport not in ("CYCLING", "RUNNING") or value is None:
                continue
            key = (date, sport.lower())
            stamp = str(raw.get("updateTimestamp") or raw.get("timestampGmt") or "")
            if key not in latest or stamp >= latest[key][0]:
                max_met = raw.get("maxMet")
                latest[key] = (stamp, {
                    "vo2max": float(value),
                    "vo2max_precise": None if max_met is None else round(max_met * 3.5, 2),
                    "garmin_activity_id": raw.get("activityId"),
                })
    return {key: row for key, (_, row) in latest.items()}


def import_vo2max(conn: sqlite3.Connection, export: Path, stats: ImportStats) -> None:
    now = _now()
    for (date, sport), row in parse_vo2max(export).items():
        conn.execute(
            "INSERT INTO vo2max (date, sport, vo2max, vo2max_precise, garmin_activity_id, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (date, sport) DO UPDATE SET vo2max = excluded.vo2max, "
            "vo2max_precise = COALESCE(excluded.vo2max_precise, vo2max.vo2max_precise), "
            "garmin_activity_id = COALESCE(excluded.garmin_activity_id, vo2max.garmin_activity_id), "
            "source = excluded.source, updated_at = excluded.updated_at",
            (date, sport, row["vo2max"], row["vo2max_precise"], row["garmin_activity_id"], SOURCE, now),
        )
        stats.vo2max += 1


# --- FIT streams -----------------------------------------------------------


@dataclass
class FitRide:
    start_epoch: float
    records: list[tuple]  # (t, power, hr, cadence, speed, distance, altitude, lat, lon)


def _fit_reader(data: bytes):
    import fitdecode  # type: ignore[import-untyped]

    return fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED)


def _fit_time_created(data: bytes) -> datetime | None:
    """Returns time_created for activity FIT files, None for every other kind."""
    import fitdecode

    with _fit_reader(data) as fr:
        for frame in fr:
            if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.name == "file_id":
                if frame.get_value("type", fallback=None) != "activity":
                    return None
                return frame.get_value("time_created", fallback=None)
    return None


def _value(frame, *names):
    for name in names:
        value = frame.get_value(name, fallback=None)
        if value is not None:
            return value
    return None


def parse_fit_ride(data: bytes) -> FitRide | None:
    import fitdecode

    session_start = None
    samples: dict[datetime, tuple] = {}
    with _fit_reader(data) as fr:
        for frame in fr:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "session" and session_start is None:
                session_start = frame.get_value("start_time", fallback=None)
            elif frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue
                lat = frame.get_value("position_lat", fallback=None)
                lon = frame.get_value("position_long", fallback=None)
                samples[ts] = (
                    frame.get_value("power", fallback=None),
                    frame.get_value("heart_rate", fallback=None),
                    frame.get_value("cadence", fallback=None),
                    _value(frame, "enhanced_speed", "speed"),
                    frame.get_value("distance", fallback=None),
                    _value(frame, "enhanced_altitude", "altitude"),
                    None if lat is None else lat * SEMICIRCLE_TO_DEG,
                    None if lon is None else lon * SEMICIRCLE_TO_DEG,
                )
    if not samples:
        return None
    start = session_start or min(samples)
    by_t: dict[int, tuple] = {}
    for ts, values in samples.items():
        t = int((ts - start).total_seconds())
        if t >= 0:
            by_t[t] = values
    records = [(t, *values) for t, values in sorted(by_t.items())]
    return FitRide(start_epoch=start.timestamp(), records=records) if records else None


def power_bests(records: list[tuple]) -> dict[int, float]:
    """Mean-maximal power per duration; seconds without a sample count as 0 W."""
    if not records or all(r[1] is None for r in records):
        return {}
    length = records[-1][0] + 1
    watts = [0] * length
    for r in records:
        watts[r[0]] = r[1] or 0
    prefix = [0]
    for w in watts:
        prefix.append(prefix[-1] + w)
    bests = {}
    for d in POWER_BEST_DURATIONS:
        if d > length:
            break
        bests[d] = round(max(prefix[i + d] - prefix[i] for i in range(length - d + 1)) / d, 1)
    return bests


def _iter_fit_files(export: Path) -> Iterator[tuple[Path, str, bytes]]:
    for zpath in sorted(export.glob("DI-Connect-Uploaded-Files/*.zip")):
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                if not info.is_dir() and info.filename.lower().endswith(".fit"):
                    yield zpath, info.filename, zf.read(info)


def import_streams(conn: sqlite3.Connection, export: Path, since: datetime, stats: ImportStats) -> None:
    rides = conn.execute(
        "SELECT id, start_utc FROM activity WHERE sport_group = 'cycling' AND start_utc >= ? ORDER BY start_utc",
        (since.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    starts = [_iso_to_epoch(s) for _, s in rides]
    ids = [i for i, _ in rides]
    have_stream = {r[0] for r in conn.execute("SELECT DISTINCT activity_id FROM activity_stream")}
    stats.rides_streams_already_present = len(have_stream & set(ids))

    # Pass 1: match activity FITs to rides; keep only the location of the longest file per ride.
    best: dict[int, tuple[int, Path, str]] = {}
    earliest = since - timedelta(days=1)
    for zpath, member, data in _iter_fit_files(export):
        stats.fit_files_scanned += 1
        created = _fit_time_created(data)
        if created is None or created < earliest:
            continue
        ride = parse_fit_ride(data)
        if ride is None:
            continue
        pos = bisect.bisect_left(starts, ride.start_epoch - FIT_MATCH_TOLERANCE_S)
        if pos == len(starts) or abs(starts[pos] - ride.start_epoch) > FIT_MATCH_TOLERANCE_S:
            stats.ride_fits_unmatched += 1  # runs, golf, or rides outside the window
            continue
        activity_id = ids[pos]
        if activity_id in have_stream:
            continue
        if activity_id not in best or len(ride.records) > best[activity_id][0]:
            best[activity_id] = (len(ride.records), zpath, member)

    # Pass 2: re-read the winners and store them.
    for activity_id, (_, zpath, member) in best.items():
        with zipfile.ZipFile(zpath) as zf:
            ride = parse_fit_ride(zf.read(member))
        if ride is None:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO activity_stream "
            "(activity_id, t, power, hr, cadence, speed_mps, distance_m, altitude_m, lat, lon) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(activity_id, *r) for r in ride.records],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO activity_power_best (activity_id, duration_s, watts) VALUES (?, ?, ?)",
            [(activity_id, d, w) for d, w in power_bests(ride.records).items()],
        )
        stats.rides_with_streams += 1


# --- entry point -----------------------------------------------------------


def run_import(export: Path, db_path: Path | str, streams_since: datetime | None) -> ImportStats:
    export = Path(export)
    stats = ImportStats()
    conn = connect(db_path)
    try:
        with conn:
            import_activities(conn, export, stats)
            import_wellness(conn, export, stats)
            import_weights(conn, export, stats)
            import_vo2max(conn, export, stats)
        if streams_since is not None:
            with conn:
                import_streams(conn, export, streams_since, stats)
    finally:
        conn.close()
    return stats


def table_counts(db_path: Path | str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("activity", "wellness_day", "weight", "vo2max", "activity_stream", "activity_power_best")
        }
    finally:
        conn.close()
