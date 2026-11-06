"""Static facts about the goal event and the training block.

These are inputs, not measurements: the race, its targets and the course come
from the organiser and from Lapo, so they live here instead of in SQLite.
"""

from __future__ import annotations

from datetime import date

RACE_NAME = "Strade Bianche Gran Fondo"
RACE_ROUTE = "Short route"
RACE_PLACE = "Siena"
RACE_DAY = date(2027, 3, 7)
DISTANCE_KM = 87
CLIMB_M = 1350

TARGETS = [
    {"tier": "A", "time": "3:15", "label": "Stretch"},
    {"tier": "B", "time": "3:30", "label": "Goal"},
    {"tier": "C", "time": "3:45", "label": "Floor"},
]

# Monday of the first training week; the block runs to race week.
PLAN_START = date(2026, 11, 2)
WEEKS_TOTAL = (RACE_DAY - PLAN_START).days // 7 + 1

START_WEIGHT_KG = 82.0
TARGET_WEIGHT_KG = 74.0
CTL_RACE_TARGET = 70

# Elevation (m) every km, smoothed, from the official short-route GPX.
COURSE_PROFILE = [
    321, 303, 279, 260, 249, 255, 261, 254, 244, 225, 212, 208, 217, 227, 228,
    221, 207, 200, 199, 201, 197, 193, 187, 187, 197, 214, 229, 234, 231, 224,
    231, 258, 301, 337, 336, 314, 276, 253, 235, 229, 219, 203, 196, 195, 209,
    212, 206, 206, 203, 201, 193, 181, 183, 188, 195, 200, 200, 199, 191, 192,
    202, 209, 225, 227, 241, 233, 235, 240, 255, 276, 278, 283, 288, 299, 314,
    321, 326, 332, 326, 326, 333, 329, 332, 316, 294, 291, 289,
]

# Placeholder positions until the official sector list is loaded.
SECTORS = [
    {"n": 1, "start": 8, "end": 11},
    {"n": 2, "start": 17, "end": 22},
    {"n": 3, "start": 31, "end": 35},
    {"n": 4, "start": 46, "end": 52},
    {"n": 5, "start": 60, "end": 64},
    {"n": 6, "start": 70, "end": 73},
]
FUEL = [
    {"km": 20, "what": "Gel"},
    {"km": 40, "what": "Bar + bottle"},
    {"km": 58, "what": "Gel"},
    {"km": 75, "what": "Gel + caffeine"},
]
POSITIONS_ARE_PLACEHOLDERS = True
