"""Garmin API activity sync: KD6 dedup, FIT storage, retry, deadline, lock."""

from __future__ import annotations

import gzip
import json
import time
from datetime import UTC, date, datetime

import pytest

from coach.db.migrate import migrate
from coach.ingest import garmin_sync as gs
from coach.ingest import intervals_sync
from coach.ingest.garmin_export import ImportStats, import_activities
from coach.integrations.garmin import GarminError
from coach.store.db import connect_write
from tests.test_fit_ride import make_rich_fit
from tests.test_intervals_sync import act, ok

GID = 25074559291
D0, D1 = date(2026, 11, 13), date(2026, 11, 14)
FIT = make_rich_fit()


@pytest.fixture
def conn():
    c = connect_write(":memory:")
    migrate(c)
    yield c
    c.close()


def summary(gid=GID, start="2026-11-14 09:00:00", type_key="road_biking", **kw):
    return {
        "activityId": gid, "activityName": "Morning ride", "startTimeGMT": start,
        "startTimeLocal": "2026-11-14 10:00:00", "activityType": {"typeKey": type_key},
        "distance": 1000.0, "duration": 120.0, "elapsedDuration": 130.0, "movingDuration": 120.0,
        "elevationGain": 20.0, "averageSpeed": 8.3, "averageHR": 140, "maxHR": 160,
        "avgPower": 150, "maxPower": 250, "normPower": 160, "max20MinPower": 155,
        "averageBikingCadenceInRevPerMinute": 90, "calories": 100, "activityTrainingLoad": 30.5,
        "aerobicTrainingEffect": 2.1, "anaerobicTrainingEffect": 0.4,
        "trainingStressScore": 25.0, "intensityFactor": 0.8,
        "hrTimeInZone_1": 20.0, "powerTimeInZone_1": 5.0,
        "ownerId": 42, "ownerFullName": "Someone", "ownerProfileImageUrlLarge": "https://x",
        **kw,
    }


class FakeGarmin:
    def __init__(self, acts, fit_ok=True):
        self.acts = acts
        self.fit_ok = fit_ok
        self.fit_calls = 0

    def activities(self, oldest, newest):
        return self.acts

    def activity(self, activity_id):
        return {}

    def fit(self, activity_id):
        self.fit_calls += 1
        if not self.fit_ok:
            raise GarminError("GET /download-service -> 503: ")
        return FIT


def run(conn, client, fit_dir, **kw):
    return gs.sync_activities(client, conn, fit_dir, D0, D1, **kw)


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def intervals_row(conn, gid=None, start="2026-11-14T09:01:00Z", icu="i9", avg_power=999.0):
    conn.execute(
        "INSERT INTO activity (garmin_activity_id, icu_activity_id, source, sport, sport_group, start_utc,"
        " avg_power, name, updated_at) VALUES (?, ?, 'intervals', 'Ride', 'cycling', ?, ?, 'I', 'x')",
        (gid, icu, start, avg_power),
    )
    conn.commit()
    return q(conn, "SELECT id FROM activity WHERE icu_activity_id = ?", (icu,))[0][0]


def test_new_ride_full_detail(conn, tmp_path):
    res = run(conn, FakeGarmin([summary()]), tmp_path)
    assert res == {"new": 1, "updated": 0, "fit_pending": 0, "errors": 0}
    row = conn.execute(
        "SELECT id, source, sport, sport_group, indoor, start_utc, start_local, distance_m, duration_s,"
        " elapsed_s, avg_power, norm_power, tss, intensity_factor, work_kj, ftp_used, lthr_used,"
        " max_hr_used, weight_kg_used, fit_path, raw_json FROM activity WHERE garmin_activity_id = ?",
        (GID,)).fetchone()
    (act_id, source, sport, group, indoor, start, local, dist, dur, elapsed, avg_p, np_, tss, if_,
     work, ftp, lthr, max_hr, weight, fit_path, raw) = row
    assert (source, sport, group, indoor) == ("garmin_api", "road_biking", "cycling", 0)
    assert (start, local) == ("2026-11-14T09:00:00Z", "2026-11-14T10:00:00")
    assert (dist, dur, elapsed, avg_p, np_) == (1000.0, 120.0, 130.0, 150, 160)
    assert tss == pytest.approx(25.0) and if_ == pytest.approx(0.8) and work == pytest.approx(18.0)
    assert (ftp, lthr, max_hr, weight) == (159, 166, 202, 84.5)
    assert "owner" not in raw.lower()
    assert json.loads(raw)["activityId"] == GID
    assert gzip.decompress((tmp_path / fit_path).read_bytes()) == FIT
    assert q(conn, "SELECT count(*) FROM activity_stream WHERE activity_id = ?", (act_id,))[0][0] == 120
    assert q(conn, "SELECT idx, start_s, trigger FROM activity_lap WHERE activity_id = ? ORDER BY idx",
             (act_id,)) == [(0, 0, "distance"), (1, 70, "session_end")]
    assert q(conn, "SELECT count(*) FROM activity_zone_time WHERE activity_id = ? AND kind = 'power'",
             (act_id,))[0][0] == 7
    assert q(conn, "SELECT watts FROM activity_power_best WHERE activity_id = ? AND duration_s = 5",
             (act_id,)) == [(200.0,)]
    assert q(conn, "SELECT value FROM sync_state WHERE key = ?", (gs.ACTIVITIES_KEY,))


def test_non_ride_gets_summary_only(conn, tmp_path):
    fake = FakeGarmin([summary(gid=7, type_key="running")])
    assert run(conn, fake, tmp_path)["new"] == 1
    assert fake.fit_calls == 0
    assert q(conn, "SELECT sport_group, fit_path FROM activity") == [("running", None)]


def test_intervals_row_with_garmin_id_updated_in_place(conn, tmp_path):
    act_id = intervals_row(conn, gid=GID)
    res = run(conn, FakeGarmin([summary()]), tmp_path)
    assert (res["new"], res["updated"]) == (0, 1)
    assert q(conn, "SELECT id, icu_activity_id, garmin_activity_id, source, avg_power FROM activity") == [
        (act_id, "i9", GID, "garmin_api", 150)]


def test_intervals_row_without_garmin_id_matched_by_time(conn, tmp_path):
    act_id = intervals_row(conn, start="2026-11-14T09:03:00Z")
    run(conn, FakeGarmin([summary()]), tmp_path)
    assert q(conn, "SELECT id, icu_activity_id, garmin_activity_id, source FROM activity") == [
        (act_id, "i9", GID, "garmin_api")]
    assert q(conn, "SELECT count(*) FROM activity_lap WHERE activity_id = ?", (act_id,))[0][0] == 2


def test_intervals_row_outside_tolerance_not_matched(conn, tmp_path):
    intervals_row(conn, start="2026-11-14T09:10:00Z")
    assert run(conn, FakeGarmin([summary()]), tmp_path)["new"] == 1
    assert q(conn, "SELECT count(*) FROM activity")[0][0] == 2


def test_intervals_resync_does_not_regress(conn, tmp_path):
    run(conn, FakeGarmin([summary()]), tmp_path)
    payload = [act(i="i9", external_id=str(GID), start="2026-11-14T09:00:00", icu_average_watts=999)]
    intervals_sync.sync_activities(ok(payload), conn, D0, D1)
    assert q(conn, "SELECT count(*), source, avg_power, icu_activity_id FROM activity") == [
        (1, "garmin_api", 150, "i9")]


def test_export_reimport_keeps_garmin_api(conn, tmp_path):
    fit_dir = tmp_path / "fit"
    run(conn, FakeGarmin([summary()]), fit_dir)
    export = tmp_path / "export"
    path = export / "DI-Connect-Fitness/x_0_summarizedActivities.json"
    path.parent.mkdir(parents=True)
    start_ms = datetime(2026, 11, 14, 9, tzinfo=UTC).timestamp() * 1000
    path.write_text(json.dumps([{"summarizedActivitiesExport": [
        {"activityId": GID, "startTimeGmt": start_ms, "activityType": "road_biking", "avgPower": 999,
         "name": "export"}]}]))
    import_activities(conn, export, ImportStats())
    conn.commit()
    assert q(conn, "SELECT source, avg_power, name FROM activity") == [("garmin_api", 150, "Morning ride")]


def test_fit_failure_leaves_summary_then_retry_fills(conn, tmp_path):
    res = run(conn, FakeGarmin([summary()], fit_ok=False), tmp_path)
    assert res == {"new": 1, "updated": 0, "fit_pending": 1, "errors": 1}
    assert q(conn, "SELECT fit_path, avg_power FROM activity") == [(None, 150)]
    assert q(conn, "SELECT count(*) FROM activity_lap")[0][0] == 0
    res = run(conn, FakeGarmin([summary()]), tmp_path)
    assert res == {"new": 0, "updated": 1, "fit_pending": 0, "errors": 0}
    assert q(conn, "SELECT fit_path IS NOT NULL FROM activity") == [(1,)]
    assert q(conn, "SELECT count(*) FROM activity_lap")[0][0] == 2


def test_second_run_is_idempotent(conn, tmp_path):
    fake = FakeGarmin([summary()])
    run(conn, fake, tmp_path)
    res = run(conn, fake, tmp_path)
    assert res["new"] == 0 and res["fit_pending"] == 0
    assert fake.fit_calls == 1
    assert q(conn, "SELECT count(*) FROM activity")[0][0] == 1
    assert q(conn, "SELECT count(*) FROM activity_stream")[0][0] == 120


def test_deadline_leaves_fit_pending(conn, tmp_path):
    fake = FakeGarmin([summary(), summary(gid=GID + 1, start="2026-11-14 15:00:00")])
    res = run(conn, fake, tmp_path, deadline=time.monotonic() - 1)
    assert res == {"new": 2, "updated": 0, "fit_pending": 2, "errors": 0}
    assert fake.fit_calls == 0
    assert q(conn, "SELECT count(*) FROM activity WHERE fit_path IS NULL")[0][0] == 2


def test_lock_held_raises_and_stale_lock_is_taken(conn, tmp_path):
    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO sync_state (key, value, updated_at) VALUES (?, 'other', ?)", (gs.LOCK_KEY, fresh))
    conn.commit()
    with pytest.raises(gs.SyncLocked):
        run(conn, FakeGarmin([summary()]), tmp_path)
    assert q(conn, "SELECT count(*) FROM activity")[0][0] == 0
    conn.execute("UPDATE sync_state SET updated_at = '2026-01-01T00:00:00Z' WHERE key = ?", (gs.LOCK_KEY,))
    conn.commit()
    assert run(conn, FakeGarmin([summary()]), tmp_path)["new"] == 1
    assert q(conn, "SELECT count(*) FROM sync_state WHERE key = ?", (gs.LOCK_KEY,))[0][0] == 0
