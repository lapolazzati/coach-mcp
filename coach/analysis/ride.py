"""Pure metrics over a ride's 1 s stream.

The stream is any iterable of records shaped like ``activity_stream`` rows (dicts or
``sqlite3.Row`` with ``t, power, hr, speed_mps, distance_m, altitude_m``) or like
``FitRide.records`` tuples ``(t, power, hr, cadence, speed, distance, altitude, lat, lon)``.
Each record stands for one second of moving time; a gap > PAUSE_GAP_S between records is
a pause, and no rolling window spans one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import pairwise
from typing import Any, NamedTuple

from coach.ingest.garmin_export import PAUSE_GAP_S

NP_WINDOW_S = 30
SEGMENT_BY = ("time", "distance", "altitude", "lap")

# field -> (accepted mapping keys, FitRide tuple index)
_FIELDS = {
    "t": (("t",), 0),
    "power": (("power",), 1),
    "hr": (("hr",), 2),
    "speed": (("speed_mps", "speed"), 4),
    "distance": (("distance_m", "distance"), 5),
    "altitude": (("altitude_m", "altitude"), 6),
}


class Sample(NamedTuple):
    t: float
    power: float | None
    hr: float | None
    speed: float | None
    distance: float | None
    altitude: float | None


def _field(record: Any, name: str) -> Any:
    keys, index = _FIELDS[name]
    if hasattr(record, "keys"):
        available = record.keys()
        for key in keys:
            if key in available:
                return record[key]
        return None
    return record[index] if len(record) > index else None


def _samples(stream: Iterable[Any]) -> list[Sample]:
    out = [Sample(*(_field(r, name) for name in _FIELDS)) for r in stream]
    return sorted(out, key=lambda s: s.t)


def _runs(samples: Sequence[Sample]) -> list[list[Sample]]:
    """Split at pauses (gaps > PAUSE_GAP_S)."""
    runs: list[list[Sample]] = []
    for s in samples:
        if runs and s.t - runs[-1][-1].t <= PAUSE_GAP_S:
            runs[-1].append(s)
        else:
            runs.append([s])
    return runs


def _moving_s(samples: Sequence[Sample]) -> int:
    return int(sum(run[-1].t - run[0].t + 1 for run in _runs(samples)))


def _np(samples: Sequence[Sample]) -> float | None:
    if not any(s.power is not None for s in samples):
        return None
    fourth: list[float] = []
    for run in _runs(samples):
        start = int(run[0].t)
        watts = [0.0] * (int(run[-1].t) - start + 1)  # short in-run gaps count as 0 W
        for s in run:
            watts[int(s.t) - start] = float(s.power or 0)
        prefix = [0.0]
        for w in watts:
            prefix.append(prefix[-1] + w)
        # windows never reach back past the run start: the first 29 s of a run average what exists
        for i in range(1, len(prefix)):
            lo = max(0, i - NP_WINDOW_S)
            fourth.append(((prefix[i] - prefix[lo]) / (i - lo)) ** 4)
    return (sum(fourth) / len(fourth)) ** 0.25


def np(stream: Iterable[Any]) -> float | None:
    """Normalized Power: 30 s trailing mean (never spanning a pause), 4th-power mean, 4th root.

    Windows at the start of each pause-free run average the seconds available so far, so short
    runs between stops still count (matches Garmin within 1 W on a stop-and-go ride).
    """
    return _np(_samples(stream))


def _work_kj(samples: Sequence[Sample]) -> float | None:
    powers = [s.power for s in samples if s.power is not None]
    return sum(powers) / 1000 if powers else None


def work_kj(stream: Iterable[Any]) -> float | None:
    """Mechanical work in kJ, each record = 1 s."""
    return _work_kj(_samples(stream))


def _in_zone(value: float, low: float | None, high: float | None) -> bool:
    return (low is None or value > low) and (high is None or value <= high)


def _zone_time(samples: Sequence[Sample], zones: Iterable[Any]) -> list[dict[str, Any]]:
    out = []
    for z in zones:
        kind, low, high = z["kind"], z["low"], z["high"]
        if kind not in ("hr", "power"):
            raise ValueError(f"unknown zone kind {kind!r}")
        values = [getattr(s, kind) for s in samples]
        seconds = sum(1 for v in values if v is not None and _in_zone(v, low, high))
        out.append({"kind": kind, "zone": z["zone"], "low": low, "high": high, "seconds": seconds})
    return out


def zone_time(stream: Iterable[Any], zones: Iterable[Any]) -> list[dict[str, Any]]:
    """Seconds per zone; zones carry activity_zone_time ``kind, zone, low, high`` (low < v <= high)."""
    return _zone_time(_samples(stream), zones)


def _drift(samples: Sequence[Sample]) -> float | None:
    with_hr = [s for s in samples if s.hr]
    half = len(with_hr) // 2
    if half == 0:
        return None
    ratios = []
    for part in (with_hr[:half], with_hr[half:]):
        power = sum(s.power or 0 for s in part) / len(part)
        hr = sum(s.hr for s in part if s.hr) / len(part)
        ratios.append(power / hr)
    first, second = ratios
    if first == 0:
        return None
    return (first - second) / first * 100


def drift(stream: Iterable[Any]) -> float | None:
    """Pa:HR decoupling in %: (P/HR first half - second half) / first half."""
    return _drift(_samples(stream))


def _crossing(samples: Sequence[Sample], value: float, after: int = 0) -> int | None:
    alts = [(i, s.altitude) for i, s in enumerate(samples) if i >= after and s.altitude is not None]
    if alts and alts[0][1] == value:
        return alts[0][0]
    for (_, a0), (i, a1) in pairwise(alts):
        if (a0 - value) * (a1 - value) <= 0:
            return i
    return None


def _bounds(samples: Sequence[Sample], by: str, from_: float, to: float,
            laps: Sequence[Any] | None) -> tuple[float, float]:
    end = samples[-1].t + 1
    if by == "time":
        if from_ >= to:
            raise ValueError(f"from_ ({from_}) must be < to ({to})")
        if to > end:
            raise ValueError(f"to={to} s is beyond the stream end ({end:g} s)")
        return from_, to
    if by == "distance":
        if from_ >= to:
            raise ValueError(f"from_ ({from_}) must be < to ({to})")
        i0 = next((i for i, s in enumerate(samples) if s.distance is not None and s.distance >= from_), None)
        i1 = None if i0 is None else next(
            (i for i, s in enumerate(samples) if i > i0 and s.distance is not None and s.distance >= to), None)
        if i0 is None or i1 is None:
            raise ValueError(f"distance {to if i0 is not None else from_} m is beyond the stream")
        return samples[i0].t, samples[i1].t
    if by == "altitude":
        i0 = _crossing(samples, from_)
        if i0 is None:
            raise ValueError(f"altitude {from_:g} m is never crossed")
        i1 = _crossing(samples, to, after=i0 + 1)
        if i1 is None:
            raise ValueError(f"altitude {to:g} m is never crossed after {from_:g} m")
        return samples[i0].t, samples[i1].t
    # lap: from_..to inclusive, 0-based activity_lap.idx
    if not laps:
        raise ValueError("by='lap' needs laps")
    starts = {int(lap["idx"]): float(lap["start_s"]) for lap in laps}
    if from_ > to:
        raise ValueError(f"from_ lap ({from_}) must be <= to lap ({to})")
    for idx in (from_, to):
        if int(idx) not in starts:
            raise ValueError(f"lap {idx} not found (laps {min(starts)}..{max(starts)})")
    return starts[int(from_)], starts.get(int(to) + 1, end)


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def segment(stream: Iterable[Any], by: str, from_: float, to: float, above_w: float | None = None,
            zones: Iterable[Any] | None = None, weight_kg: float | None = None,
            laps: Sequence[Any] | None = None) -> dict[str, Any]:
    """Stats for a slice of the ride.

    by='time': [from_, to) in stream seconds. by='distance': first record at >= from_ m to the
    first after it at >= to m. by='altitude': first crossing of from_ m, then the first crossing
    of to m after it. by='lap': laps from_..to inclusive (0-based idx; needs ``laps`` rows with
    ``idx, start_s``). Raises ValueError on an empty or invalid range.
    """
    if by not in SEGMENT_BY:
        raise ValueError(f"by must be one of {SEGMENT_BY}, got {by!r}")
    samples = _samples(stream)
    if not samples:
        raise ValueError("stream is empty")
    t0, t1 = _bounds(samples, by, from_, to, laps)
    sel = [s for s in samples if t0 <= s.t < t1]
    if not sel:
        raise ValueError(f"no records between {t0:g} s and {t1:g} s")
    end = next((s for s in samples if s.t >= t1), samples[-1])
    span = [s for s in samples if t0 <= s.t <= end.t]

    dists = [s.distance for s in (sel[0], end) if s.distance is not None]
    distance_m = dists[-1] - dists[0] if len(dists) == 2 else None
    alts = [s.altitude for s in span if s.altitude is not None]
    gain_m = sum(max(b - a, 0.0) for a, b in pairwise(alts)) if alts else None

    duration_s = _moving_s(sel)
    powers = [s.power for s in sel if s.power is not None]
    hrs = [s.hr for s in sel if s.hr is not None]
    avg_power = sum(powers) / len(powers) if powers else None
    return {
        "by": by,
        "start_s": t0,
        "end_s": t1,
        "elapsed_s": t1 - t0,
        "duration_s": duration_s,
        "distance_m": _r(distance_m),
        "gain_m": _r(gain_m),
        "avg_power": _r(avg_power),
        "np": _r(_np(sel)),
        "work_kj": _r(_work_kj(sel)),
        "w_per_kg": _r(avg_power / weight_kg) if avg_power is not None and weight_kg else None,
        "vam": _r(gain_m * 3600 / duration_s) if gain_m is not None else None,
        "avg_hr": _r(sum(hrs) / len(hrs)) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "zone_time": _zone_time(sel, zones) if zones is not None else None,
        "pct_above_w": _r(100 * sum(1 for p in powers if p > above_w) / len(powers))
        if above_w is not None and powers else None,
        "drift": _r(_drift(sel)),
    }
