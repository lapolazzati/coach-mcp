"""Garmin Personal API client + wellness sync, against anonymized fixtures shaped like real responses."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from coach.db.migrate import migrate
from coach.ingest import garmin_sync as g
from coach.integrations.garmin import TOKEN_PATH, GarminClient, GarminError, windows
from coach.jobs import garmin as garmin_job
from coach.store.db import connect_write

D1, D3 = date(2026, 11, 4), date(2026, 11, 6)


def jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


# Trimmed from real responses; identifiers dropped, values altered.
SLEEP = [
    {"calendarDate": None, "deepSleepSeconds": None, "sleepStartTimestampGMT": None},
    {"calendarDate": "2026-11-04", "sleepStartTimestampGMT": 1793744773000, "sleepEndTimestampGMT": 1793772168000,
     "deepSleepSeconds": 3600, "lightSleepSeconds": 17600, "remSleepSeconds": 1800, "awakeSleepSeconds": 4300,
     "avgSleepStress": 25.0, "sleepTimeSeconds": 23000, "userProfilePK": 1},
    {"calendarDate": "2026-11-06", "sleepStartTimestampGMT": 1793914827000, "sleepEndTimestampGMT": 1793942427000,
     "deepSleepSeconds": 3900, "lightSleepSeconds": 18900, "remSleepSeconds": 1900, "awakeSleepSeconds": 2700,
     "avgSleepStress": -1.0},
]
SUMMARIES = {"data": [
    {"calendarDate": "2026-11-04", "stress": {"avgLevel": 29, "maxLevel": 86, "qualifier": "balanced"},
     "bodyBattery": {"minValue": 26, "maxValue": 61, "chargedValue": 40}},
    {"calendarDate": "2026-11-05", "stress": {"avgLevel": -2}, "bodyBattery": {"minValue": 15, "maxValue": 97}},
    {"calendarDate": "2026-11-06", "stress": {}, "bodyBattery": {}},
]}
HRV = {"hrvSummaries": [
    {"calendarDate": "2026-11-04", "weeklyAvg": 59, "lastNightAvg": 54},
    {"calendarDate": "2026-11-05", "weeklyAvg": 60, "lastNightAvg": 62},
]}


def router(calls: list[httpx.Request], token: str | None = None, overrides: dict | None = None):
    tok = token or jwt(4_000_000_000)

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        path = req.url.path
        if overrides and path in overrides:
            return overrides[path](req)
        if path == TOKEN_PATH:
            assert req.headers["Authorization"] == "ApiKey secret-key"
            return httpx.Response(200, text=tok)
        assert req.headers["Authorization"] == f"Bearer {tok}"
        if path.startswith("/sleep-service/"):
            return httpx.Response(200, json=SLEEP)
        if path.startswith("/usersummary-service/"):
            return httpx.Response(200, json=SUMMARIES)
        if path.startswith("/hrv-service/"):
            return httpx.Response(200, json=HRV)
        return httpx.Response(404)

    return handle


def client(handler, **kw) -> GarminClient:
    return GarminClient("secret-key", transport=httpx.MockTransport(handler), backoff_s=0, retries=2, **kw)


@pytest.fixture
def conn():
    c = connect_write(":memory:")
    migrate(c)
    yield c
    c.close()


def row(conn, day: str, cols: str):
    return conn.execute(f"SELECT {cols} FROM wellness_day WHERE date = ?", (day,)).fetchone()


def test_windows_cap_at_28_days():
    spans = list(windows(date(2026, 1, 1), date(2026, 3, 1)))
    assert spans[0] == (date(2026, 1, 1), date(2026, 1, 28))
    assert spans[-1][1] == date(2026, 3, 1)
    assert all((b - a).days < 28 for a, b in spans)
    assert list(windows(D3, D3)) == [(D3, D3)]


def test_sync_maps_garmin_only_columns(conn):
    calls: list[httpx.Request] = []
    assert g.sync_wellness(client(router(calls)), conn, D1, D3) == 3
    assert row(conn, "2026-11-04", "sleep_deep_secs, sleep_light_secs, sleep_rem_secs, sleep_awake_secs, "
               "sleep_start_utc, sleep_end_utc, sleep_avg_stress, avg_stress, body_battery_high, "
               "body_battery_low, hrv_weekly_avg") == (
        3600, 17600, 1800, 4300, "2026-11-03T22:26:13Z", "2026-11-04T06:02:48Z", 25.0, 29, 61, 26, 59)
    # -1 / -2 mean "not enough data"
    assert row(conn, "2026-11-06", "sleep_avg_stress, avg_stress") == (None, None)
    assert row(conn, "2026-11-05", "avg_stress, body_battery_high, hrv_weekly_avg") == (None, 97, 60)
    # one token exchange for the whole run
    assert sum(1 for c in calls if c.url.path == TOKEN_PATH) == 1
    assert conn.execute("SELECT value FROM sync_state WHERE key = ?", (g.WELLNESS_KEY,)).fetchone() is not None


def test_never_touches_intervals_columns(conn):
    conn.execute("INSERT INTO wellness_day (date, sleep_secs, sleep_score, hrv_ms, resting_hr, steps, ctl, atl, "
                 "updated_at) VALUES ('2026-11-04', 25000, 80, 55.5, 48, 9000, 40.0, 50.0, 'x')")
    conn.commit()
    g.sync_wellness(client(router([])), conn, D1, D3)
    assert row(conn, "2026-11-04", "sleep_secs, sleep_score, hrv_ms, resting_hr, steps, ctl, atl") == (
        25000, 80, 55.5, 48, 9000, 40.0, 50.0)
    assert row(conn, "2026-11-04", "sleep_deep_secs")[0] == 3600


def test_null_keeps_stored_value_and_new_value_refreshes(conn):
    conn.execute("INSERT INTO wellness_day (date, avg_stress, body_battery_high, updated_at) "
                 "VALUES ('2026-11-06', 33, 80, 'x'), ('2026-11-04', 10, 50, 'x')")
    conn.commit()
    g.sync_wellness(client(router([])), conn, D1, D3)
    assert row(conn, "2026-11-06", "avg_stress, body_battery_high") == (33, 80)  # Garmin had nothing
    assert row(conn, "2026-11-04", "avg_stress, body_battery_high") == (29, 61)  # Garmin refreshed


def test_out_of_range_and_empty_days_create_no_rows(conn):
    g.sync_wellness(client(router([])), conn, D3, D3)
    assert [r[0] for r in conn.execute("SELECT date FROM wellness_day")] == ["2026-11-06"]


def test_token_reexchanged_after_401(conn):
    calls: list[httpx.Request] = []
    state = {"n": 0}
    tokens = [jwt(4_000_000_000).replace(".s", ".a"), jwt(4_000_000_000)]

    def token(req):
        state["n"] += 1
        return httpx.Response(200, text=tokens[min(state["n"], 2) - 1])

    inner = router(calls, token=tokens[1], overrides={TOKEN_PATH: token})

    def handle(req):
        if req.headers.get("Authorization") == f"Bearer {tokens[0]}":
            calls.append(req)
            return httpx.Response(401)
        return inner(req)

    assert g.sync_wellness(client(handle), conn, D1, D3) == 3
    assert state["n"] == 2


def test_expired_token_is_renewed():
    now = {"t": 1_000_000.0}
    calls: list[httpx.Request] = []
    tok = jwt(now["t"] + 3600)
    c = client(router(calls, token=tok), clock=lambda: now["t"])
    c.hrv_summaries(D1, D3)
    c.hrv_summaries(D1, D3)
    assert sum(1 for r in calls if r.url.path == TOKEN_PATH) == 1
    now["t"] += 3600 - 60  # inside the renewal margin
    c.hrv_summaries(D1, D3)
    assert sum(1 for r in calls if r.url.path == TOKEN_PATH) == 2


def test_token_failure_does_not_leak_key_and_skips_sync_state(conn):
    def handle(req):
        return httpx.Response(403, text="bad key secret-key")

    with pytest.raises(GarminError) as err:
        g.sync_wellness(client(handle), conn, D1, D3)
    assert "secret-key" not in str(err.value)
    assert conn.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 0


def test_retries_5xx_then_raises(conn):
    calls: list[httpx.Request] = []

    def boom(req):
        return httpx.Response(503)

    with pytest.raises(GarminError):
        g.sync_wellness(client(router(calls, overrides={"/sleep-service/sleep/dailySleepSummariesByDate": boom})),
                        conn, D1, D3)
    assert sum(1 for r in calls if r.url.path.startswith("/sleep-service/")) == 3


def test_from_settings_requires_key():
    from types import SimpleNamespace
    with pytest.raises(GarminError):
        GarminClient.from_settings(SimpleNamespace(garmin_personal_api_key=None))


def test_job_skips_without_client_and_syncs_three_days(conn):
    now = datetime(2026, 11, 6, 8, 0, tzinfo=ZoneInfo("Europe/Rome"))
    assert garmin_job.run(conn, None, now) == {"skipped": "garmin not configured"}
    calls: list[httpx.Request] = []
    assert garmin_job.run(conn, client(router(calls)), now) == {"wellness": 3}
    sleep_call = next(r for r in calls if r.url.path.startswith("/sleep-service/"))
    assert sleep_call.url.params["startDate"] == "2026-11-04"
    assert sleep_call.url.params["endDate"] == "2026-11-06"
