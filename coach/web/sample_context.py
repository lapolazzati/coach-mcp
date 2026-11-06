"""SAMPLE DATA for the dashboard template.

Every number here is invented for layout work, except the course elevation
shape, which is resampled from the official GPX (SB_MF_26_CRT.gpx, 1 km steps).
Sector and fuelling positions are placeholders until the real sector list is
loaded. U12 replaces this module with queries; the dict shape is the contract.
"""

from __future__ import annotations

import copy

from coach.web.course import COURSE_PROFILE

_DAY = {"D": "done", "M": "moved", "X": "missed", "R": "rest", "P": "planned", "T": "today"}


def _adherence(weeks: list[str]) -> dict:
    """One string per week, Mon..Sun, coded with _DAY. Moved counts as done."""
    days = [[_DAY[ch] for ch in wk] for wk in weeks]
    flat = [d for wk in days for d in wk]
    done = sum(d in ("done", "moved") for d in flat)
    return {"days": days, "done": done, "planned": done + flat.count("missed")}


SAMPLE: dict = {
    "sample": True,
    "today": {"label": "Tue 3 Nov", "week": 1, "weeks_total": 18},
    "session": {
        "kind": "ride",
        "title": "Zwift 45′",
        "subtitle": "Sweet spot 3×8",
        "slot": "07:00",
        "duration_min": 45,
        "tss": 52,
        "rating": 2,  # perceived load, 1–5
        "detail": "Z2 base with three 8-minute efforts at 88–92% FTP.",
        "why": "Slept 7h10, HRV in range. First call at 09:30, so the morning slot holds.",
        "status": "planned",
        "in_zwift": True,
        # Set when a job eased the session (R17); `advice` when it could not because Lapo locked it (KTD9).
        "eased": {"at": "06:50", "reason": "HRV 18% under baseline, short sleep."},
        "advice": None,
    },
    "readiness": {"available": False, "days_collected": 9, "days_needed": 14, "status": "amber",
                  "flags": ["HRV below baseline", "Short sleep"]},
    "goal": {
        "name": "Strade Bianche Gran Fondo",
        "route": "Short route",
        "place": "Siena",
        "date": "Sun 7 Mar 2027",
        "days_to_go": 124,
        "distance_km": 87,
        "climb_m": 1350,
        "sectors": 6,
        "projected": "3:38",
        "gap_min": 8,  # positive = slower than target B
        "targets": [
            {"tier": "A", "time": "3:15", "label": "Stretch"},
            {"tier": "B", "time": "3:30", "label": "Goal"},
            {"tier": "C", "time": "3:45", "label": "Floor"},
        ],
        # Projected finish (minutes) at the end of each past week, oldest first.
        "projected_history": [236, 233, 231, 229, 226, 224, 221, 218],
    },
    "course": {
        "profile": COURSE_PROFILE,
        "km_total": 87,
        "positions_are_placeholders": True,
        "sectors": [
            {"n": 1, "start": 8, "end": 11},
            {"n": 2, "start": 17, "end": 22},
            {"n": 3, "start": 31, "end": 35},
            {"n": 4, "start": 46, "end": 52},
            {"n": 5, "start": 60, "end": 64},
            {"n": 6, "start": 70, "end": 73},
        ],
        "fuel": [
            {"km": 20, "what": "Gel"},
            {"km": 40, "what": "Bar + bottle"},
            {"km": 58, "what": "Gel"},
            {"km": 75, "what": "Gel + caffeine"},
        ],
    },
    "week": [
        {"dow": "Mon", "date": "2", "kind": "gym", "title": "Gym", "slot": "12:30", "status": "done"},
        {"dow": "Tue", "date": "3", "kind": "ride", "title": "Sweet spot 3×8", "slot": "07:00", "status": "planned", "today": True, "note": "Eased: HRV 18% under baseline"},
        {"dow": "Wed", "date": "4", "kind": "ride", "title": "Z2 endurance 50′", "slot": "", "status": "moved", "note": "Train 08:30 · moved to Thu"},
        {"dow": "Thu", "date": "5", "kind": "ride", "title": "Z2 endurance 50′", "slot": "18:30", "status": "planned"},
        {"dow": "Fri", "date": "6", "kind": "gym", "title": "Gym", "slot": "12:30", "status": "planned"},
        {"dow": "Sat", "date": "7", "kind": "rest", "title": "Rest", "slot": "", "status": "planned"},
        {"dow": "Sun", "date": "8", "kind": "ride", "title": "Long ride 2h30", "slot": "09:00", "status": "planned"},
    ],
    "fitness": {
        "ctl": [38, 39, 39, 40, 41, 41, 42, 43, 43, 44, 44, 45, 45, 46],
        "atl": [42, 48, 40, 37, 45, 52, 46, 41, 49, 55, 47, 43, 50, 47],
        "ctl_now": 46, "atl_now": 47, "tsb_now": -1,
        "ctl_race_target": 70,
    },
    "weight": {
        "series": [82.0, 81.8, 81.9, 81.5, 81.3, 81.4, 81.0, 80.8, 80.9, 80.6, 80.4],
        "now": 80.4, "start": 82.0, "target": 74.0, "per_week": -0.55,
    },
    "volume": [
        {"label": "W40", "planned": 3.5, "done": 3.2},
        {"label": "W41", "planned": 4.0, "done": 4.1},
        {"label": "W42", "planned": 4.0, "done": 2.6},
        {"label": "W43", "planned": 4.5, "done": 4.4},
        {"label": "W44", "planned": 4.5, "done": 4.0},
        {"label": "W45", "planned": 5.0, "done": 0.8},
    ],
    "adherence": _adherence([
        "DDRDRRD", "DDRXRRD", "DRDDRRD", "DDRDRRX", "DMRDRRD", "DDRDRXD",
        "DDRDRRD", "XDRDRRD", "DDRMRRD", "DDRDRRD", "DXRDRRD", "DDRDRRD",
        "DDRDXRD", "DDRMRRD", "DDRDRRD", "DXRDRXD", "DDRDRRD", "DTMPPRP",
    ]),
    "power": {
        "ftp": 238, "wkg": 2.96,
        "curve": [
            {"dur": "5s", "now": 812, "prev": 790},
            {"dur": "1m", "now": 402, "prev": 395},
            {"dur": "5m", "now": 291, "prev": 280},
            {"dur": "20m", "now": 251, "prev": 244},
            {"dur": "60m", "now": 226, "prev": 221},
        ],
    },
    "rides": [
        {
            "date": "Sun 1 Nov", "title": "Long ride, Chianti loop", "km": 62.4, "time": "2:24", "np": 196,
            "segments": [
                {"name": "Salita di Vagliagli", "time": "11:42", "prev": "12:31", "delta": -49},
                {"name": "Sterrato Pievasciata", "time": "6:05", "prev": "5:58", "delta": 7},
            ],
        },
        {
            "date": "Thu 29 Oct", "title": "Zwift, Over/unders 4×6", "km": 28.1, "time": "0:50", "np": 221,
            "segments": [],
        },
    ],
    "review": {
        "week": "W44 · from Mon 26 Oct",
        "prose": "Solid week: four of five sessions done, the long ride held Z2 throughout. Sleep dipped midweek.",
        "numbers": [
            {"label": "Hours", "value": "4.0"},
            {"label": "Tss", "value": "268"},
            {"label": "Sessions done", "value": "4"},
            {"label": "Sessions planned", "value": "5"},
        ],
    },
    "supplements": [
        {"name": "Vitamin D", "dose": "2000 IU", "timing": "morning", "taken": True},
        {"name": "Creatine", "dose": "5 g", "timing": "with meal", "taken": False},
        {"name": "Magnesium", "dose": "300 mg", "timing": "evening", "taken": False},
    ],
    "notes": [
        {"date": "Mon 2 Nov", "text": "Left knee a bit stiff after gym, fine on the bike.", "tags": ["knee"]},
        {"date": "Sun 1 Nov", "text": "Long ride felt easy, could have pushed the last hour.", "tags": []},
        {"date": "Thu 29 Oct", "text": "Late dinner, slept badly.", "tags": ["sleep"]},
    ],
}


def sample_context(state: str = "full") -> dict:
    """Return a SAMPLE context. `state="day1"` renders the honest empty state."""
    ctx = copy.deepcopy(SAMPLE)
    if state == "day1":
        ctx["goal"]["projected"] = None
        ctx["goal"]["projected_history"] = []
        ctx["fitness"] = None
        ctx["weight"]["series"] = [82.0]
        ctx["weight"]["now"] = 82.0
        ctx["weight"]["per_week"] = None
        ctx["volume"] = []
        ctx["adherence"] = None
        ctx["power"] = None
        ctx["rides"] = []
        ctx["readiness"].update(days_collected=0, status="unknown", flags=[])
        ctx["session"].update(eased=None)
        ctx["review"] = None
        ctx["supplements"] = []
        ctx["notes"] = []
    return ctx
