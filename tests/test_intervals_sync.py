from __future__ import annotations

from datetime import date

import httpx
import pytest

from coach.db.migrate import migrate
from coach.ingest import intervals_sync as s
from coach.integrations.intervals import IntervalsClient, IntervalsError
from coach.store.db import connect_write


@pytest.fixture
def conn():
    c = connect_write(":memory:")
    migrate(c)
    yield c
    c.close()


def client(handler) -> IntervalsClient:
    return IntervalsClient("k", "i1", transport=httpx.MockTransport(handler), backoff_s=0, retries=2)


def ok(payload):
    return client(lambda req: httpx.Response(200, json=payload))


def day(d, **kw):
    return {"id": f"2026-11-{d:02d}", **kw}


D1, D3 = date(2026, 11, 4), date(2026, 11, 6)


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def test_three_days_upsert_and_mapping(conn):
    payload = [
        day(4, sleepSecs=25000, sleepScore=80, hrv=55.5, restingHR=48, ctl=40.2, atl=50.1, weight=71.234),
        day(5, sleepSecs=26000, hrv=60.0),
        day(6, sleepSecs=27000, bodyBatteryHigh=88, steps=100),
    ]
    assert s.sync_wellness(ok(payload), conn, D1, D3) == 3
    assert q(conn, "SELECT count(*) FROM wellness_day")[0][0] == 3
    r = q(conn, "SELECT sleep_secs, sleep_score, hrv_ms, resting_hr, ctl, atl FROM wellness_day WHERE date='2026-11-04'")[0]
    assert r == (25000, 80, 55.5, 48, 40.2, 50.1)
    assert q(conn, "SELECT body_battery_high FROM wellness_day WHERE date='2026-11-06'")[0][0] == 88
    assert q(conn, "SELECT body_battery_high FROM wellness_day WHERE date='2026-11-05'")[0][0] is None
    assert q(conn, "SELECT date, kg, source FROM weight") == [("2026-11-04", 71.23, "intervals")]


def test_missing_field_keeps_existing(conn):
    conn.execute("INSERT INTO wellness_day (date, hrv_ms, resting_hr, updated_at) VALUES ('2026-11-06', 62, 47, 'x')")
    conn.commit()
    s.sync_wellness(ok([day(6, sleepSecs=20000, hrv=None, restingHR=49)]), conn, D3, D3)
    assert q(conn, "SELECT hrv_ms, resting_hr, sleep_secs FROM wellness_day")[0] == (62, 49, 20000)


def test_idempotent_rerun(conn):
    p = [day(5, sleepSecs=1, hrv=50.0, weight=70.0), day(6, sleepSecs=2)]
    s.sync_wellness(ok(p), conn, D1, D3)
    snap = q(conn, "SELECT date, sleep_secs, hrv_ms FROM wellness_day ORDER BY date")
    s.sync_wellness(ok(p), conn, D1, D3)
    assert q(conn, "SELECT date, sleep_secs, hrv_ms FROM wellness_day ORDER BY date") == snap
    assert q(conn, "SELECT count(*) FROM weight")[0][0] == 1


def test_sleep_seen_set_once(conn):
    s.sync_wellness(ok([day(6, hrv=50.0)]), conn, D3, D3)
    assert q(conn, "SELECT count(*) FROM readiness_day")[0][0] == 0  # no sleep yet
    s.sync_wellness(ok([day(6, sleepSecs=25000)]), conn, D3, D3)
    first = q(conn, "SELECT sleep_seen_at FROM readiness_day WHERE date='2026-11-06'")[0][0]
    assert first
    conn.execute("UPDATE readiness_day SET sleep_seen_at='2020-01-01T00:00:00Z'")
    conn.commit()
    s.sync_wellness(ok([day(6, sleepSecs=25500)]), conn, D3, D3)
    assert q(conn, "SELECT sleep_seen_at FROM readiness_day")[0][0] == "2020-01-01T00:00:00Z"


def test_429_then_200(conn):
    calls = iter([httpx.Response(429), httpx.Response(200, json=[day(6, sleepSecs=9)])])
    assert s.sync_wellness(client(lambda req: next(calls)), conn, D3, D3) == 1
    assert q(conn, "SELECT value FROM sync_state WHERE key=?", (s.WELLNESS_KEY,))[0][0]


def test_persistent_500_keeps_last_ok(conn):
    conn.execute("INSERT INTO sync_state (key, value, updated_at) VALUES (?, 'old', 'old')", (s.WELLNESS_KEY,))
    conn.commit()
    with pytest.raises(IntervalsError):
        s.sync_wellness(client(lambda req: httpx.Response(500)), conn, D3, D3)
    assert q(conn, "SELECT value FROM sync_state WHERE key=?", (s.WELLNESS_KEY,))[0][0] == "old"


def garmin_row(conn, gid=111, start="2026-11-05T07:00:00Z", sport_group="cycling", avg_power=200.0):
    conn.execute(
        "INSERT INTO activity (garmin_activity_id, source, sport, sport_group, start_utc, avg_power, name, updated_at)"
        " VALUES (?, 'garmin_export', 'road_biking', ?, ?, ?, 'G', 'x')",
        (gid, sport_group, start, avg_power),
    )
    conn.commit()


def act(i="i9", start="2026-11-05T07:01:00", **kw):
    return {"id": i, "type": "Ride", "start_date": start, "start_date_local": "2026-11-05T08:01:00",
            "moving_time": 3600, "icu_average_watts": 250, "icu_training_load": 70, "name": "I", **kw}


def test_activity_matched_by_external_id(conn):
    garmin_row(conn)
    s.sync_activities(ok([act(external_id="111", start="2026-11-05T12:00:00")]), conn, D1, D3)
    rows = q(conn, "SELECT garmin_activity_id, icu_activity_id, avg_power, training_load, name FROM activity")
    assert rows == [(111, "i9", 200.0, 70.0, "G")]  # garmin value kept, gap filled


def test_activity_matched_by_time_not_duplicated(conn):
    garmin_row(conn)
    s.sync_activities(ok([act()]), conn, D1, D3)
    s.sync_activities(ok([act()]), conn, D1, D3)
    assert q(conn, "SELECT garmin_activity_id, icu_activity_id FROM activity") == [(111, "i9")]


def test_activity_no_match_inserts_and_refreshes(conn):
    garmin_row(conn, start="2026-11-05T15:00:00Z")
    s.sync_activities(ok([act()]), conn, D1, D3)
    assert q(conn, "SELECT count(*) FROM activity")[0][0] == 2
    s.sync_activities(ok([act(moving_time=4000)]), conn, D1, D3)
    assert q(conn, "SELECT moving_s FROM activity WHERE icu_activity_id='i9'")[0][0] == 4000
    assert q(conn, "SELECT count(*) FROM activity")[0][0] == 2


def test_other_sport_not_matched(conn):
    garmin_row(conn, sport_group="running")
    s.sync_activities(ok([act()]), conn, D1, D3)
    assert q(conn, "SELECT count(*) FROM activity")[0][0] == 2


def test_activities_failure_keeps_last_ok(conn):
    with pytest.raises(IntervalsError):
        s.sync_activities(client(lambda req: httpx.Response(503)), conn, D1, D3)
    assert q(conn, "SELECT count(*) FROM sync_state WHERE key=?", (s.ACTIVITIES_KEY,))[0][0] == 0
