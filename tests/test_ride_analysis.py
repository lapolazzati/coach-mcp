"""coach.analysis.ride: pure metrics over the 1 s stream (table-driven, synthetic streams)."""

import sqlite3

import pytest

from coach.analysis.ride import (
    DRIFT_MIN_S,
    coggan_zones,
    drift,
    intensity,
    moving_s,
    np,
    power_summary,
    segment,
    work_kj,
    zone_time,
)


def rec(t, power=None, hr=None, speed=None, distance=None, altitude=None):
    """FitRide.records layout: (t, power, hr, cadence, speed, distance, altitude, lat, lon)."""
    return (t, power, hr, 90, speed, distance, altitude, None, None)


def steady(watts, seconds, start=0, hr=140):
    return [rec(start + i, watts, hr) for i in range(seconds)]


def np_ref(watts):
    """Reference NP over one contiguous 1 s run (partial windows for the first 29 s)."""
    rolling = [sum(watts[max(0, i - 29):i + 1]) / (i + 1 - max(0, i - 29)) for i in range(len(watts))]
    return (sum(r**4 for r in rolling) / len(rolling)) ** 0.25


def ride():
    """600 s: flat 150 W, climb 300 W (+0.5 m/s), descent 100 W (-0.5 m/s); 10 m/s throughout."""
    out = []
    for t in range(600):
        if t < 200:
            power, hr, alt = 150, 120, 100.0
        elif t < 400:
            power, hr, alt = 300, 160, 100 + 0.5 * (t - 200)
        else:
            power, hr, alt = 100, 130, 200 - 0.5 * (t - 400)
        out.append(rec(t, power, hr, 10.0, 10.0 * t, alt))
    return out


LAPS = [{"idx": 0, "start_s": 0.0}, {"idx": 1, "start_s": 200.0}, {"idx": 2, "start_s": 400.0}]
POWER_ZONES = [
    {"kind": "power", "zone": 1, "low": None, "high": 200},
    {"kind": "power", "zone": 2, "low": 200, "high": 400},
]
HR_ZONES = [
    {"kind": "hr", "zone": 1, "low": None, "high": 125},
    {"kind": "hr", "zone": 2, "low": 125, "high": 150},
    {"kind": "hr", "zone": 3, "low": 150, "high": None},
]

# 40 s at 100 W, a 60 s pause, 40 s at 300 W: no window mixes the two efforts.
PAUSED = steady(100, 40) + steady(300, 40, start=100)
PAUSED_NP = ((40 * 100**4 + 40 * 300**4) / 80) ** 0.25
SURGES = [0] * 60 + [400] * 60 + [200] * 45


@pytest.mark.parametrize(("stream", "expected"), [
    (steady(250, 120), 250.0),
    ([rec(i, w) for i, w in enumerate(SURGES)], np_ref(SURGES)),
    (PAUSED, PAUSED_NP),
    ([rec(i, None if i % 10 == 0 else 200) for i in range(120)],
     np_ref([0 if i % 10 == 0 else 200 for i in range(120)])),  # missing power counts as 0 W
    (steady(250, 10), 250.0),  # shorter than one window
    ([rec(i) for i in range(120)], None),  # no power at all
    ([], None),
], ids=["steady", "surges", "pause-splits-windows", "none-is-zero", "short", "no-power", "empty"])
def test_np(stream, expected):
    result = np(stream)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=0.05)


@pytest.mark.parametrize(("stream", "expected"), [
    (steady(200, 1000), 200.0),
    (PAUSED, (100 * 40 + 300 * 40) / 1000),  # pause adds no work
    ([rec(i, None if i % 2 else 200) for i in range(100)], 10.0),
    ([rec(i) for i in range(10)], None),
])
def test_work_kj(stream, expected):
    assert work_kj(stream) == (None if expected is None else pytest.approx(expected))


@pytest.mark.parametrize(("zones", "expected"), [
    (POWER_ZONES, {("power", 1): 400, ("power", 2): 200}),  # 150 and 100 W -> z1, 300 W -> z2
    (HR_ZONES, {("hr", 1): 200, ("hr", 2): 200, ("hr", 3): 200}),
    (POWER_ZONES + HR_ZONES, {("power", 1): 400, ("power", 2): 200,
                              ("hr", 1): 200, ("hr", 2): 200, ("hr", 3): 200}),
])
def test_zone_time(zones, expected):
    rows = zone_time(ride(), zones)
    assert {(r["kind"], r["zone"]): r["seconds"] for r in rows} == expected
    assert [(r["low"], r["high"]) for r in rows] == [(z["low"], z["high"]) for z in zones]


def test_zone_time_power_bounds_are_low_exclusive_high_inclusive():
    stream = [rec(0, 200), rec(1, 201), rec(2, 400), rec(3, 401)]
    rows = zone_time(stream, POWER_ZONES + [{"kind": "power", "zone": 3, "low": 400, "high": None}])
    assert [r["seconds"] for r in rows] == [1, 2, 1]


# 300 s at 200 W, 100 s coasting (0 W), 1 s dropout (None), 99 s at 200 W
COASTY = steady(200, 300) + steady(0, 100, start=300) + [rec(400, None, 140)] + steady(200, 99, start=401)


@pytest.mark.parametrize(("stream", "expected"), [
    (steady(200, 100), {"avg_power": 200.0, "avg_power_nonzero": 200.0, "coasting_pct": 0.0}),
    # true avg counts coasting and dropouts as 0 W over moving time; nonzero skips both
    (COASTY, {"avg_power": 200 * 399 / 500, "avg_power_nonzero": 200.0, "coasting_pct": 20.0}),
    (PAUSED, {"avg_power": 200.0, "avg_power_nonzero": 200.0, "coasting_pct": 0.0}),  # pause is not moving
    ([rec(i) for i in range(10)], {"avg_power": None, "avg_power_nonzero": None, "coasting_pct": None}),
    ([], {"avg_power": None, "avg_power_nonzero": None, "coasting_pct": None}),
], ids=["steady", "coasting-and-none", "pause", "no-power", "empty"])
def test_power_summary(stream, expected):
    result = power_summary(stream)
    for key, value in expected.items():
        assert result[key] == (None if value is None else pytest.approx(value, abs=0.05)), key


# pedalling 200 W @ 140 bpm, then pedalling 200 W @ 150 bpm; long 0 W descents in the second half
# would fake a big drift if coasting counted
DESCENT = (steady(200, 600, hr=140) + steady(0, 600, start=600, hr=110)
           + steady(200, 600, start=1200, hr=150))


@pytest.mark.parametrize(("stream", "expected"), [
    (steady(200, 600, hr=140), 0.0),
    (DESCENT, (200 / 140 - 200 / 150) / (200 / 140) * 100),
    (steady(200, DRIFT_MIN_S - 1, hr=140) + steady(0, 600, start=DRIFT_MIN_S, hr=140), None),  # <10 min pedalling
    (steady(200, 300, hr=140) + steady(200, 300, start=300, hr=150), (200 / 140 - 200 / 150) / (200 / 140) * 100),
    (steady(200, 300, hr=150) + steady(200, 300, start=300, hr=140), (200 / 150 - 200 / 140) / (200 / 150) * 100),
    ([rec(i, 200) for i in range(600)], None),  # no HR
    (steady(200, 1), None),
], ids=["steady", "descent-ignored", "short-pedalling", "half-up", "half-down", "no-hr", "one-sample"])
def test_drift(stream, expected):
    result = drift(stream)
    assert result == (None if expected is None else pytest.approx(expected, abs=0.05))


CLIMB = {"duration_s": 200, "distance_m": 2000.0, "gain_m": 100.0, "avg_power": 300.0, "np": 300.0,
         "w_per_kg": 4.0, "vam": 1800.0, "avg_hr": 160.0, "max_hr": 160, "pct_above_w": 100.0,
         "s_above_w": 200, "pct_above_hr": 100.0, "s_above_hr": 200, "avg_power_nonzero": 300.0,
         "coasting_pct": 0.0}


@pytest.mark.parametrize(("by", "from_", "to", "expected"), [
    ("time", 0, 200, {"duration_s": 200, "distance_m": 2000.0, "gain_m": 0.0, "avg_power": 150.0,
                      "np": 150.0, "w_per_kg": 2.0, "vam": 0.0, "avg_hr": 120.0, "pct_above_w": 0.0}),
    ("time", 100, 300, {"duration_s": 200, "avg_power": 225.0, "pct_above_w": 50.0, "max_hr": 160}),
    ("distance", 2000, 4000, CLIMB),
    ("lap", 1, 1, CLIMB),
    ("lap", 1, 2, {"duration_s": 400, "avg_power": 200.0, "distance_m": 3990.0}),  # ends at the last record
    ("altitude", 120, 180, {**CLIMB, "duration_s": 120, "distance_m": 1200.0, "gain_m": 60.0,
                            "s_above_w": 120, "s_above_hr": 120}),
    # first crossing of 190 is on the climb (t=380), then 150 on the way down (t=500)
    ("altitude", 190, 150, {"duration_s": 120, "gain_m": 10.0, "distance_m": 1200.0}),
])
def test_segment(by, from_, to, expected):
    result = segment(ride(), by, from_, to, above_w=250, above_hr=150, zones=POWER_ZONES, weight_kg=75, laps=LAPS)
    for key, value in expected.items():
        assert result[key] == pytest.approx(value), key
    assert set(CLIMB) | {"zone_time", "drift", "drift_note"} <= set(result)
    assert result["drift"] is None and result["drift_note"] == "<10 min pedalling"


def test_segment_zone_time_and_optional_fields():
    result = segment(ride(), "time", 100, 300, zones=POWER_ZONES)
    assert [r["seconds"] for r in result["zone_time"]] == [100, 100]
    bare = segment(ride(), "time", 100, 300)
    assert bare["zone_time"] is None
    assert bare["pct_above_w"] is None
    assert bare["w_per_kg"] is None
    assert bare["pct_above_hr"] is None and bare["s_above_hr"] is None


def test_segment_above_thresholds_are_strict_and_drift_on_long_segment():
    # 50 s at 150 bpm (not above 150), 50 s at 151; 200 W then 201 W
    stream = steady(200, 50, hr=150) + steady(201, 50, start=50, hr=151)
    result = segment(stream, "time", 0, 100, above_w=200, above_hr=150)
    assert (result["s_above_w"], result["pct_above_w"]) == (50, 50.0)
    assert (result["s_above_hr"], result["pct_above_hr"]) == (50, 50.0)
    long = segment(DESCENT, "time", 0, 1800)
    assert long["drift"] == pytest.approx((200 / 140 - 200 / 150) / (200 / 140) * 100, abs=0.05)
    assert long["drift_note"] is None
    assert long["coasting_pct"] == pytest.approx(100 / 3, abs=0.05)


def test_segment_excludes_pause_from_duration():
    stream = [rec(t, 200, 140, 10.0, 10.0 * t, 100.0) for t in [*range(100), *range(160, 260)]]
    result = segment(stream, "time", 0, 260)
    assert result["duration_s"] == 200
    assert result["elapsed_s"] == 260
    assert result["avg_power"] == 200.0


def test_segment_accepts_db_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE s (t, power, hr, cadence, speed_mps, distance_m, altitude_m, lat, lon)")
    conn.executemany("INSERT INTO s VALUES (?,?,?,?,?,?,?,?,?)", ride())
    rows = conn.execute("SELECT * FROM s ORDER BY t").fetchall()
    dicts = [dict(r) for r in rows]
    expected = segment(ride(), "distance", 2000, 4000, weight_kg=75)
    assert segment(rows, "distance", 2000, 4000, weight_kg=75) == expected
    assert segment(dicts, "distance", 2000, 4000, weight_kg=75) == expected
    assert np(rows) == np(ride())


@pytest.mark.parametrize(("stream", "by", "from_", "to", "laps", "match"), [
    (ride(), "time", 300, 100, None, "from"),
    (ride(), "time", 0, 10_000, None, "beyond"),
    (ride(), "distance", 0, 99_999, None, "beyond"),
    (ride(), "altitude", 100, 500, None, "500"),
    (ride(), "altitude", 50, 150, None, "50"),
    (ride(), "lap", 0, 1, None, "laps"),
    (ride(), "lap", 5, 5, LAPS, "lap 5"),
    (ride(), "lap", 2, 1, LAPS, "from"),
    (ride(), "speed", 0, 1, None, "by"),
    ([], "time", 0, 10, None, "empty"),
    (steady(200, 100) + steady(200, 100, start=200), "time", 120, 180, None, "no records"),
], ids=["reversed", "time-beyond", "distance-beyond", "alt-to-never", "alt-from-never", "no-laps",
        "lap-missing", "lap-reversed", "bad-by", "empty-stream", "inside-pause"])
def test_segment_invalid(stream, by, from_, to, laps, match):
    with pytest.raises(ValueError, match=match):
        segment(stream, by, from_, to, laps=laps)


# Coggan power zones and IF/TSS on a given FTP


def test_coggan_zones_bounds():
    zones = coggan_zones(200)
    assert [z["zone"] for z in zones] == [1, 2, 3, 4, 5, 6, 7]
    assert all(z["kind"] == "power" for z in zones)
    assert [(z["low"], z["high"]) for z in zones] == [
        (None, 110), (110, 150), (150, 180), (180, 210), (210, 240), (240, 300), (300, None)]


def test_coggan_zone_time_counts_zero_and_boundaries():
    stream = [rec(0, 0), rec(1, 110), rec(2, 111), rec(3, 300), rec(4, 301), rec(5, None)]
    seconds = {z["zone"]: z["seconds"] for z in zone_time(stream, coggan_zones(200))}
    assert seconds == {1: 2, 2: 1, 3: 0, 4: 0, 5: 0, 6: 1, 7: 1}


@pytest.mark.parametrize(("np_w", "ftp", "seconds", "expected"), [
    (200, 200, 3600, (1.0, 100.0)),
    (178.8, 190, 6419, (0.94, 157.9)),
    (100, 200, 1800, (0.5, 12.5)),
])
def test_intensity(np_w, ftp, seconds, expected):
    if_, tss = intensity(np_w, ftp, seconds)
    assert (round(if_, 2), round(tss, 1)) == expected


def test_intensity_missing_inputs():
    assert intensity(None, 200, 3600) == (None, None)
    assert intensity(200, None, 3600) == (None, None)
    assert intensity(200, 200, None) == (1.0, None)


def test_moving_s_excludes_pauses():
    assert moving_s(steady(100, 60) + steady(100, 60, start=600)) == 120
