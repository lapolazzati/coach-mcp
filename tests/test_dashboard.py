import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach.ingest.garmin_export import ensure_schema
from coach.web.app import create_app
from coach.web.context import build_context
from coach.web.env import build_env
from coach.web.sample_context import SAMPLE, sample_context

TODAY = date(2026, 11, 4)  # a Wednesday, week 1 of the plan
NOW = "2026-11-04T00:00:00"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    yield conn
    conn.close()


def _ride(db, day: date, *, name="Ride", hours=1.0, km=30.0, np=None, best20=None, start="07:30", sport="cycling"):
    cur = db.execute(
        "INSERT INTO activity (source, sport, sport_group, name, start_utc, start_local, duration_s,"
        " distance_m, norm_power, max_20min_power, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("test", "road_biking", sport, name, f"{day}T{start}:00Z", f"{day}T{start}:00",
         hours * 3600, km * 1000, np, best20, NOW),
    )
    return cur.lastrowid


def _best(db, activity_id: int, secs: int, watts: float):
    db.execute("INSERT INTO activity_power_best VALUES (?,?,?)", (activity_id, secs, watts))


def _shape(value):
    """Key structure of a context: dicts by key, lists by their first item."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return [_shape(value[0])]
    return type(value).__name__ if value is not None else None


# --- empty database --------------------------------------------------------


def test_empty_db_matches_day_one_shape(db):
    ctx = build_context(db, TODAY)
    day1 = sample_context("day1")

    assert ctx.keys() == SAMPLE.keys()
    assert ctx["sample"] is False
    for key in ("fitness", "adherence", "power"):
        assert ctx[key] is None
    assert ctx["session"] is None  # no planner yet
    for key in ("volume", "rides"):
        assert ctx[key] == []
    assert ctx["goal"]["projected"] is None
    assert ctx["goal"]["projected_history"] == []
    assert ctx["weight"] == {**day1["weight"], "start": 82.0}
    assert ctx["readiness"] == {"available": False, "days_collected": 0, "days_needed": 14}
    for key in ("today", "readiness", "goal", "course", "weight"):
        assert ctx[key].keys() == SAMPLE[key].keys(), key


def test_empty_week_says_nothing_logged_and_not_planned(db):
    week = build_context(db, TODAY)["week"]
    assert [d["dow"] for d in week] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [d["status"] for d in week] == ["rest", "rest", "planned", "planned", "planned", "planned", "planned"]
    assert week[0]["title"] == "Nothing logged"
    assert week[2]["today"] is True and week[2]["title"] == "Not planned yet"
    sample_keys = set().union(*(d.keys() for d in SAMPLE["week"]))
    assert all(d.keys() <= sample_keys for d in week)


def test_goal_and_today_come_from_config(db):
    ctx = build_context(db, TODAY)
    assert ctx["today"] == {"label": "Wed 4 Nov", "week": 1, "weeks_total": 18}
    assert ctx["goal"]["days_to_go"] == (date(2027, 3, 7) - TODAY).days
    assert ctx["goal"]["date"] == "Sun 7 Mar 2027"
    assert ctx["goal"]["distance_km"] == 87 and ctx["goal"]["climb_m"] == 1350
    assert ctx["course"]["km_total"] == 87 and len(ctx["course"]["profile"]) == 87


# --- seeded database -------------------------------------------------------


def test_week_shows_logged_activities(db):
    _ride(db, date(2026, 11, 2), name="Zwift Endurance", start="07:39")
    _ride(db, date(2026, 11, 3), name="Gym", sport="strength", start="18:05")
    week = build_context(db, TODAY)["week"]
    assert week[0] | {} == {"dow": "Mon", "date": "2", "kind": "ride", "title": "Zwift Endurance",
                            "slot": "07:39", "status": "done"}
    assert week[1]["kind"] == "gym" and week[1]["slot"] == "18:05"


def test_volume_sums_cycling_hours_per_week(db):
    _ride(db, date(2026, 11, 2), hours=1.5)
    _ride(db, date(2026, 11, 3), hours=0.5)
    _ride(db, date(2026, 10, 20), hours=2.0)
    _ride(db, date(2026, 11, 3), hours=3.0, sport="running")  # not cycling
    volume = build_context(db, TODAY)["volume"]
    assert [v["label"] for v in volume] == ["W40", "W41", "W42", "W43", "W44", "W45"]
    assert [v["done"] for v in volume] == [0, 0, 0, 2.0, 0, 2.0]
    assert all(v["planned"] is None for v in volume)


def test_weight_series_and_trend(db):
    for i, kg in enumerate([84.9, 84.2, 83.6, 82.8]):
        db.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?,?,?,?)",
                   ((TODAY - timedelta(days=28 - 7 * i)).isoformat(), kg, "test", NOW))
    weight = build_context(db, TODAY)["weight"]
    assert weight["series"] == [84.9, 84.2, 83.6, 82.8]
    assert weight["now"] == 82.8 and weight["start"] == 84.9 and weight["target"] == 74.0
    assert weight["per_week"] == pytest.approx(-0.69, abs=0.01)


def test_weight_trend_needs_two_weeks(db):
    db.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?,?,?,?)", ("2026-11-01", 83.0, "a", NOW))
    db.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?,?,?,?)", ("2026-11-03", 82.5, "a", NOW))
    assert build_context(db, TODAY)["weight"]["per_week"] is None


def test_readiness_counts_nights_with_sleep_and_hrv(db):
    for i in range(14):
        hrv = None if i == 3 else 60
        db.execute("INSERT INTO wellness_day (date, sleep_secs, hrv_ms, updated_at) VALUES (?,?,?,?)",
                   ((TODAY - timedelta(days=i)).isoformat(), 27000, hrv, NOW))
    assert build_context(db, TODAY)["readiness"] == {"available": False, "days_collected": 13, "days_needed": 14}


def test_fitness_uses_ctl_atl_only_when_present(db):
    db.execute("INSERT INTO wellness_day (date, acute_load, chronic_load, updated_at) VALUES (?,?,?,?)",
               ("2026-11-03", 400, 300, NOW))
    assert build_context(db, TODAY)["fitness"] is None  # Garmin loads are a different scale
    for i, (c, a) in enumerate([(40.2, 45.6), (41.0, 50.4)]):
        db.execute("INSERT INTO wellness_day (date, ctl, atl, updated_at) VALUES (?,?,?,?)",
                   (f"2026-10-2{i}", c, a, NOW))
    fitness = build_context(db, TODAY)["fitness"]
    assert fitness == {"ctl": [40, 41], "atl": [46, 50], "ctl_now": 41, "atl_now": 50, "tsb_now": -9,
                       "ctl_race_target": 70}


def test_power_from_bests_and_ftp_estimate(db):
    now_ride = _ride(db, date(2026, 10, 25))
    prev_ride = _ride(db, date(2026, 9, 10))
    for secs, now, prev in [(5, 800, 760), (60, 400, 390), (1200, 220, 210)]:
        _best(db, now_ride, secs, now)
        _best(db, prev_ride, secs, prev)
    _best(db, now_ride, 3600, 190)  # no prev → left out
    power = build_context(db, TODAY)["power"]
    assert power["curve"] == [{"dur": "5s", "now": 800, "prev": 760}, {"dur": "1m", "now": 400, "prev": 390},
                              {"dur": "20m", "now": 220, "prev": 210}]
    assert power["ftp"] == 209
    assert power["wkg"] == round(209 / 82.0, 2)


def test_power_falls_back_to_summary_20min(db):
    _ride(db, date(2026, 10, 25), best20=199.5)
    _ride(db, date(2026, 9, 10), best20=190)
    power = build_context(db, TODAY)["power"]
    assert power["curve"] == [{"dur": "20m", "now": 200, "prev": 190}]
    assert power["ftp"] == 190


def test_power_needs_a_comparison_window(db):
    _ride(db, date(2026, 10, 25), best20=199.5)
    assert build_context(db, TODAY)["power"] is None


def test_rides_are_latest_cycling_first(db):
    _ride(db, date(2026, 10, 30), name="Bettola", km=62.34, hours=2.25, np=180.4)
    _ride(db, date(2026, 11, 2), name=None, km=20, hours=0.75)
    _ride(db, date(2026, 11, 5), name="Future")  # after today
    rides = build_context(db, TODAY)["rides"]
    assert rides == [
        {"date": "Mon 2 Nov", "title": "Ride", "km": 20.0, "time": "0:45", "np": None, "segments": []},
        {"date": "Fri 30 Oct", "title": "Bettola", "km": 62.3, "time": "2:15", "np": 180, "segments": []},
    ]


# --- rendering -------------------------------------------------------------


def _seeded(db):
    _ride(db, date(2026, 11, 2), name="Zwift", np=170, best20=190)
    _ride(db, date(2026, 9, 20), name="Bettola", np=180, best20=199.5)
    db.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?,?,?,?)", ("2026-10-07", 82.8, "a", NOW))
    return build_context(db, TODAY)


@pytest.mark.parametrize("which", ["full", "day1", "empty", "seeded"])
def test_template_renders_under_strict_undefined(db, which):
    ctx = {"full": lambda: sample_context("full"), "day1": lambda: sample_context("day1"),
           "empty": lambda: build_context(db, TODAY), "seeded": lambda: _seeded(db)}[which]()
    html = build_env().get_template("dashboard.html").render(static_url="/static", **ctx)
    assert "Strade Bianche" in html
    assert ("Sample data" in html) is (which in ("full", "day1"))
    assert "fonts.googleapis" not in html


def test_route_serves_dashboard_and_static(tmp_path):
    path = tmp_path / "coach.sqlite"
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (date('now'), 81.5, 'a', ?)", (NOW,))
    conn.commit()
    conn.close()

    client = TestClient(create_app(path))
    res = client.get("/")
    assert res.status_code == 200
    assert "81.5" in res.text and "Sample data" not in res.text
    assert client.get("/static/dashboard.css").status_code == 200
    font = client.get("/static/fonts/barlow-condensed-800.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"


def test_route_with_missing_db_renders_day_one(tmp_path):
    res = TestClient(create_app(tmp_path / "absent.sqlite")).get("/")
    assert res.status_code == 200
    assert "Sample data" not in res.text
