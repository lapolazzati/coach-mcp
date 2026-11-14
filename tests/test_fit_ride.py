"""parse_fit_ride: laps, session totals, zone time, thresholds and pauses from a synthetic FIT."""

import struct
from datetime import UTC, datetime, timedelta

import pytest

from coach.ingest.garmin_export import parse_fit_ride
from tests.test_garmin_export import _crc, _definition, _fit_ts

START = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
FMT = {0x00: ("B", 1), 0x02: ("B", 1), 0x84: ("H", 2), 0x86: ("I", 4)}


def _message(local: int, global_num: int, fields: list[tuple]) -> bytes:
    """Definition + one data message. fields: (field_num, base_type, value or list)."""
    defs, data = [], b""
    for num, base, value in fields:
        ch, size = FMT[base]
        values = value if isinstance(value, list) else [value]
        defs.append((num, size * len(values), base))
        data += struct.pack("<" + ch * len(values), *values)
    return _definition(local, global_num, defs) + bytes([local]) + data


def _ts(seconds: float) -> int:
    return _fit_ts(START + timedelta(seconds=seconds))


def _lap(start_s, timer_s, power, trigger):
    return _message(3, 19, [
        (253, 0x86, _ts(start_s + timer_s)), (2, 0x86, _ts(start_s)),
        (7, 0x86, timer_s * 1000), (8, 0x86, timer_s * 1000), (9, 0x86, 50000),
        (15, 0x02, 140), (16, 0x02, 150), (17, 0x02, 90),
        (19, 0x84, power), (20, 0x84, power + 50), (33, 0x84, power + 5),
        (21, 0x84, 10), (22, 0x84, 2), (24, 0x00, trigger), (41, 0x86, power * timer_s),
    ])


def _zones(ref_mesg: int, hr_times: list[int], power_times: list[int]) -> bytes:
    return _message(4, 216, [
        (253, 0x86, _ts(130)), (0, 0x84, ref_mesg), (1, 0x84, 0),
        (2, 0x86, [t * 1000 for t in hr_times]),
        (5, 0x86, [t * 1000 for t in power_times]),
        (6, 0x02, [101, 121, 141, 162, 182, 202]),
        (9, 0x84, [0, 86, 120, 142, 165, 189, 239, 3713, 4000, 4000]),
        (11, 0x02, 199), (13, 0x02, 164), (15, 0x84, 158),
    ])


def make_rich_fit(with_profile: bool = True) -> bytes:
    body = _message(0, 0, [(0, 0x00, 4), (4, 0x86, _ts(0))])
    if with_profile:
        body += _message(1, 3, [(4, 0x84, 845), (8, 0x02, 48)])          # user_profile
        body += _message(2, 7, [(1, 0x02, 202), (2, 0x02, 166), (3, 0x84, 159)])  # zones_target
    body += _definition(5, 20, [(253, 4, 0x86), (7, 2, 0x84), (3, 1, 0x02), (4, 1, 0x02), (6, 2, 0x84), (5, 4, 0x86)])
    # 0..59 s at 200 W, then a 10 s pause, 70..129 s at 100 W.
    for t in [*range(60), *range(70, 130)]:
        body += struct.pack("<BIHBBHI", 5, _ts(t), 200 if t < 60 else 100, 140, 90, 8000, t * 800)
    body += _lap(0, 60, 200, 2)      # distance
    body += _lap(70, 60, 100, 7)     # session_end
    body += _zones(19, [1, 1, 1, 1, 1, 1, 1], [1] * 10)   # lap-level: must be ignored
    body += _zones(18, [10, 20, 30, 40, 20, 0, 0], [0, 5, 10, 15, 20, 25, 30, 15, 0, 0])
    body += _message(6, 18, [
        (253, 0x86, _ts(130)), (2, 0x86, _ts(0)), (5, 0x00, 2),
        (7, 0x86, 130000), (8, 0x86, 120000), (9, 0x86, 100000),
        (16, 0x02, 140), (17, 0x02, 160), (18, 0x02, 90),
        (20, 0x84, 150), (21, 0x84, 250), (34, 0x84, 160), (35, 0x84, 250), (36, 0x84, 800),
        (22, 0x84, 20), (23, 0x84, 5), (45, 0x84, 200), (48, 0x86, 18000),
    ])
    header = struct.pack("<BBHI4s", 14, 0x10, 2132, len(body), b".FIT")
    header += struct.pack("<H", _crc(header))
    data = header + body
    return data + struct.pack("<H", _crc(data))


@pytest.fixture(scope="module")
def ride():
    return parse_fit_ride(make_rich_fit())


def test_records_unchanged(ride):
    assert ride.start_epoch == START.timestamp()
    assert len(ride.records) == 120
    assert ride.records[0][:3] == (0, 200, 140)
    assert ride.records[60][0] == 70  # the gap is kept, not filled


def test_session_totals(ride):
    s = ride.session
    assert s["elapsed_s"] == 130 and s["timer_s"] == 120
    assert s["distance_m"] == 1000
    assert (s["avg_power"], s["max_power"], s["norm_power"]) == (150, 250, 160)
    assert s["intensity_factor"] == pytest.approx(0.8)
    assert s["tss"] == pytest.approx(25.0)
    assert s["work_kj"] == pytest.approx(18.0)
    assert (s["avg_hr"], s["max_hr"], s["avg_cadence"]) == (140, 160, 90)
    assert (s["ascent_m"], s["descent_m"]) == (20, 5)


def test_laps(ride):
    assert [lap["idx"] for lap in ride.laps] == [0, 1]
    first, second = ride.laps
    assert first["start_utc"] == "2026-11-14T09:00:00Z"
    assert first["start_s"] == 0 and second["start_s"] == 70
    assert first["duration_s"] == 60 and first["distance_m"] == 500
    assert (first["avg_power"], first["max_power"], first["norm_power"]) == (200, 250, 205)
    assert (first["avg_hr"], first["max_hr"], first["avg_cadence"]) == (140, 150, 90)
    assert (first["ascent_m"], first["descent_m"]) == (10, 2)
    assert first["work_kj"] == pytest.approx(12.0)
    assert first["trigger"] == "distance" and second["trigger"] == "session_end"


def test_zone_time_from_session_level_message(ride):
    hr = {z["zone"]: z for z in ride.zones if z["kind"] == "hr"}
    power = {z["zone"]: z for z in ride.zones if z["kind"] == "power"}
    # Garmin numbering: index 0 = below zone 1 (kept only when non-zero).
    assert sorted(hr) == [0, 1, 2, 3, 4, 5]
    assert hr[0] == {"kind": "hr", "zone": 0, "seconds": 10, "low": None, "high": 101}
    assert hr[2]["seconds"] == 30 and (hr[2]["low"], hr[2]["high"]) == (121, 141)
    assert hr[5]["seconds"] == 0 and (hr[5]["low"], hr[5]["high"]) == (182, 202)
    assert sorted(power) == [1, 2, 3, 4, 5, 6, 7]
    assert (power[1]["low"], power[1]["high"], power[1]["seconds"]) == (0, 86, 5)
    assert (power[7]["low"], power[7]["seconds"]) == (239, 15)
    assert sum(z["seconds"] for z in power.values()) == 120


def test_thresholds_from_profile(ride):
    assert ride.thresholds == {"ftp": 159, "lthr": 166, "max_hr": 202, "resting_hr": 48, "weight_kg": 84.5}


def test_thresholds_fall_back_to_time_in_zone():
    ride = parse_fit_ride(make_rich_fit(with_profile=False))
    assert ride.thresholds["ftp"] == 158
    assert ride.thresholds["lthr"] == 164
    assert ride.thresholds["max_hr"] == 199
    assert ride.thresholds["weight_kg"] is None


def test_pauses_from_record_gaps(ride):
    assert ride.pauses == [(59, 70)]
