import json
import sqlite3
import struct
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from coach.ingest.garmin_export import power_bests, run_import, table_counts

FIT_EPOCH = 631065600
RIDE_START = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
RIDE_START_MS = RIDE_START.timestamp() * 1000


# --- minimal FIT encoder ---------------------------------------------------


def _crc(data: bytes) -> int:
    table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    crc = 0
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = table[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ table[nibble]
    return crc


def _definition(local: int, global_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    out = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
    return out + b"".join(struct.pack("BBB", *f) for f in fields)


def _fit_ts(dt: datetime) -> int:
    return int(dt.timestamp()) - FIT_EPOCH


def make_fit(start: datetime, powers: list[int], file_type: int = 4) -> bytes:
    body = _definition(0, 0, [(0, 1, 0x00), (4, 4, 0x86)])
    body += struct.pack("<BBI", 0, file_type, _fit_ts(start))
    body += _definition(1, 20, [(253, 4, 0x86), (7, 2, 0x84), (3, 1, 0x02), (4, 1, 0x02), (6, 2, 0x84), (5, 4, 0x86)])
    for i, watts in enumerate(powers):
        body += struct.pack("<BIHBBHI", 1, _fit_ts(start + timedelta(seconds=i)), watts, 140, 90, 8000, i * 800)
    body += _definition(2, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00)])
    body += struct.pack("<BIIB", 2, _fit_ts(start + timedelta(seconds=len(powers))), _fit_ts(start), 2)
    header = struct.pack("<BBHI4s", 14, 0x10, 2132, len(body), b".FIT")
    header += struct.pack("<H", _crc(header))
    data = header + body
    return data + struct.pack("<H", _crc(data))


# --- fixture export --------------------------------------------------------


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def export(tmp_path):
    root = tmp_path / "DI_CONNECT"
    activities = [
        {
            "activityId": 111, "activityType": "road_biking", "name": "Chianti loop",
            "startTimeGmt": RIDE_START_MS, "startTimeLocal": RIDE_START_MS + 7_200_000,
            "duration": 3_600_000.0, "movingDuration": 3_500_000.0, "elapsedDuration": 3_700_000.0,
            "distance": 3_000_000.0, "elevationGain": 45_000.0, "avgSpeed": 0.83,
            "avgHr": 140.0, "avgPower": 200.0, "normPower": 215.0, "activityTrainingLoad": 120.0,
        },
        {"activityId": 222, "activityType": "golf", "startTimeGmt": RIDE_START_MS - 86_400_000},
        {"activityId": 333, "activityType": "running"},  # no start time -> skipped
    ]
    _write(root / "DI-Connect-Fitness/x_0_summarizedActivities.json", [{"summarizedActivitiesExport": activities}])
    _write(root / "DI-Connect-Wellness/a_sleepData.json", [
        {"retro": True},
        {"calendarDate": "2026-09-01", "deepSleepSeconds": 3600, "lightSleepSeconds": 14400, "remSleepSeconds": 5400,
         "awakeSleepSeconds": 600, "sleepStartTimestampGMT": "2026-08-31T21:30:00.0",
         "sleepEndTimestampGMT": "2026-09-01T05:00:00.0", "sleepScores": {"overallScore": 82}},
    ])
    _write(root / "DI-Connect-Aggregator/UDSFile_1.json", [
        {"calendarDate": "2026-09-01", "restingHeartRate": 48, "totalSteps": 9000,
         "allDayStress": {"aggregatorList": [{"type": "TOTAL", "averageStressLevel": 25},
                                             {"type": "ASLEEP", "averageStressLevel": 12}]},
         "bodyBattery": {"bodyBatteryStatList": [{"bodyBatteryStatType": "HIGHEST", "statsValue": 90},
                                                 {"bodyBatteryStatType": "LOWEST", "statsValue": 20}]}},
        {"calendarDate": "2026-09-02", "restingHeartRate": None, "totalSteps": 100,
         "allDayStress": {"aggregatorList": [{"type": "TOTAL", "averageStressLevel": -1}]}},
    ])
    _write(root / "DI-Connect-Wellness/b_healthStatusData.json", [
        {"calendarDate": "2026-09-01", "metrics": [{"type": "HR", "value": 48.0}, {"type": "HRV", "value": 61.0}]},
    ])
    _write(root / "DI-Connect-Metrics/TrainingReadinessDTO_1.json", [
        {"calendarDate": "2026-09-01", "timestamp": "2026-09-01T12:00:00.0", "score": 40, "level": "LOW",
         "inputContext": "UPDATE_REALTIME_VARIABLES"},
        {"calendarDate": "2026-09-01", "timestamp": "2026-09-01T05:10:00.0", "score": 75, "level": "HIGH",
         "inputContext": "AFTER_WAKEUP_RESET", "hrvWeeklyAverage": 58},
    ])
    day_ms = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
    _write(root / "DI-Connect-Metrics/MetricsAcuteTrainingLoad_1.json", [
        {"calendarDate": day_ms, "timestamp": day_ms + 1000, "dailyTrainingLoadAcute": 300, "dailyTrainingLoadChronic": 250},
        {"calendarDate": day_ms, "timestamp": day_ms + 9000, "dailyTrainingLoadAcute": 420, "dailyTrainingLoadChronic": 300},
    ])
    _write(root / "DI-Connect-Wellness/1_userBioMetrics.json", [
        {"metaData": {"calendarDate": "2026-09-01T00:00:00.0", "sequence": 1}, "weight": {"weight": 80500.0}},
        {"metaData": {"calendarDate": "2026-09-01T00:00:00.0", "sequence": 2}, "weight": {"weight": 80100.0}},
        {"metaData": {"calendarDate": "2026-09-03T00:00:00.0", "sequence": 3}},
    ])
    _write(root / "DI-Connect-Metrics/ActivityVo2Max_1.json", [
        {"calendarDate": "2026-09-01", "sport": "CYCLING", "subSport": "ROAD", "vo2MaxValue": 50.0,
         "activityId": 111, "timestampGmt": "2026-09-01T09:00:00.0"},
    ])
    _write(root / "DI-Connect-Metrics/MetricsMaxMetData_1.json", [
        {"calendarDate": "2026-09-01", "sport": "CYCLING", "vo2MaxValue": 51.0, "maxMet": 14.6,
         "updateTimestamp": "2026-09-01T10:00:00.0"},  # later update for the same day wins
        {"calendarDate": "2026-08-20", "sport": "RUNNING", "vo2MaxValue": 47.0, "maxMet": 13.4,
         "updateTimestamp": "2026-08-20T08:00:00.0"},
        {"calendarDate": "2026-08-21", "sport": "RUNNING", "vo2MaxValue": None},  # no value -> skipped
    ])
    uploads = root / "DI-Connect-Uploaded-Files"
    uploads.mkdir(parents=True)
    with zipfile.ZipFile(uploads / "UploadedFiles_0-_Part1.zip", "w") as zf:
        zf.writestr("u_1.fit", make_fit(RIDE_START, [100] * 10))
        zf.writestr("u_2.fit", make_fit(RIDE_START + timedelta(seconds=30), [100] * 50 + [300] * 10))  # longer copy
        zf.writestr("u_3.fit", make_fit(RIDE_START, [999] * 5, file_type=32))  # monitoring, ignored
        zf.writestr("u_4.fit", make_fit(RIDE_START - timedelta(days=3), [150] * 20))  # no matching activity
    return root


SINCE = RIDE_START - timedelta(days=30)


def _rows(db, sql):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_activity_units_and_keys(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    stats = run_import(export, db, SINCE)
    assert stats.activities == 2 and stats.activities_skipped == 1
    ride = _rows(db, "SELECT * FROM activity WHERE garmin_activity_id = 111")[0]
    assert ride["start_utc"] == "2026-09-01T07:00:00Z"
    assert ride["start_local"] == "2026-09-01T09:00:00"
    assert ride["sport_group"] == "cycling" and ride["indoor"] == 0
    assert ride["duration_s"] == 3600 and ride["moving_s"] == 3500
    assert ride["distance_m"] == 30_000 and ride["elevation_gain_m"] == 450
    assert ride["avg_speed_mps"] == pytest.approx(8.3)
    assert json.loads(ride["raw_json"])["name"] == "Chianti loop"


def test_wellness_merges_sources(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    run_import(export, db, None)
    day = _rows(db, "SELECT * FROM wellness_day WHERE date = '2026-09-01'")[0]
    assert day["sleep_secs"] == 23400 and day["sleep_score"] == 82
    assert day["sleep_start_utc"] == "2026-08-31T21:30:00Z"
    assert day["resting_hr"] == 48 and day["avg_stress"] == 25
    assert (day["body_battery_high"], day["body_battery_low"]) == (90, 20)
    assert day["hrv_ms"] == 61.0
    assert (day["readiness_score"], day["readiness_level"], day["hrv_weekly_avg"]) == (75, "HIGH", 58)
    assert (day["acute_load"], day["chronic_load"], day["acwr"]) == (420, 300, 1.4)
    next_day = _rows(db, "SELECT * FROM wellness_day WHERE date = '2026-09-02'")[0]
    assert next_day["avg_stress"] is None and next_day["steps"] == 100


def test_weight_keeps_last_sequence(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    run_import(export, db, None)
    assert _rows(db, "SELECT date, kg, source FROM weight") == [
        {"date": "2026-09-01", "kg": 80.1, "source": "garmin_export"}
    ]



def test_vo2max_one_row_per_day_and_sport(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    stats = run_import(export, db, None)
    assert stats.vo2max == 2
    assert _rows(db, "SELECT date, sport, vo2max, vo2max_precise, garmin_activity_id FROM vo2max ORDER BY date") == [
        {"date": "2026-08-20", "sport": "running", "vo2max": 47.0, "vo2max_precise": 46.9, "garmin_activity_id": None},
        {"date": "2026-09-01", "sport": "cycling", "vo2max": 51.0, "vo2max_precise": 51.1, "garmin_activity_id": None},
    ]

def test_streams_pick_longest_matching_fit(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    stats = run_import(export, db, SINCE)
    assert stats.rides_with_streams == 1 and stats.ride_fits_unmatched == 1
    stream = _rows(db, "SELECT * FROM activity_stream ORDER BY t")
    assert len(stream) == 60
    assert stream[0]["power"] == 100 and stream[0]["hr"] == 140
    assert stream[0]["speed_mps"] == pytest.approx(8.0)
    assert stream[1]["distance_m"] == pytest.approx(8.0)
    bests = {r["duration_s"]: r["watts"] for r in _rows(db, "SELECT * FROM activity_power_best")}
    assert bests[5] == 300 and bests[15] == pytest.approx((5 * 100 + 10 * 300) / 15, abs=0.1)
    assert 60 not in bests or bests[60] > 0


def test_streams_outside_window_are_skipped(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    run_import(export, db, RIDE_START + timedelta(days=1))
    assert table_counts(db)["activity_stream"] == 0


def test_rerun_is_idempotent(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    run_import(export, db, SINCE)
    first = table_counts(db)
    stats = run_import(export, db, SINCE)
    assert table_counts(db) == first
    assert stats.rides_with_streams == 0 and stats.rides_streams_already_present == 1


def test_rerun_does_not_blank_other_sources(export, tmp_path):
    db = tmp_path / "coach.sqlite"
    run_import(export, db, None)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE wellness_day SET ctl = 55.0 WHERE date = '2026-09-01'")
    conn.close()
    run_import(export, db, None)
    assert _rows(db, "SELECT ctl FROM wellness_day WHERE date = '2026-09-01'")[0]["ctl"] == 55.0


def test_power_bests_fill_gaps_with_zero():
    records = [(0, 200, None, None, None, None, None, None, None), (9, 200, None, None, None, None, None, None, None)]
    assert power_bests(records) == {5: 40.0}
    assert power_bests([(0, None, 120, None, None, None, None, None, None)]) == {}
