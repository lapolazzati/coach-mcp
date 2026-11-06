"""A planned session, independent of where it is delivered.

Rides target %FTP only (Zwift and the Edge both read it). Runs target pace or
HR zone. Gym sessions carry a duration and no steps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Sport = Literal["ride", "run", "gym"]


class Step(BaseModel):
    duration_s: int = Field(gt=0)
    # Ride: %FTP. A ramp sets both low and high and ramp=True.
    power_lo: int | None = None
    power_hi: int | None = None
    ramp: bool = False
    # Run: "Z2" style HR zone, or pace in seconds per km.
    hr_zone: int | None = Field(default=None, ge=1, le=5)
    pace_s_per_km: int | None = None
    cadence: int | None = None
    label: str | None = None


class Repeat(BaseModel):
    times: int = Field(ge=2)
    steps: list[Step]
    label: str | None = None


class Session(BaseModel):
    external_id: str  # stable id we own; intervals.icu upserts on it
    sport: Sport
    start: datetime  # local wall time
    name: str
    steps: list[Step | Repeat] = []
    duration_s: int | None = None  # gym, or a ride with no structure
    rationale: str | None = None
    indoor: bool = True

    @model_validator(mode="after")
    def _targets_match_sport(self) -> Session:
        for step in _flat(self.steps):
            if self.sport == "ride" and (step.hr_zone or step.pace_s_per_km):
                raise ValueError("ride steps target %FTP only")
            if self.sport == "run" and (step.power_lo or step.power_hi):
                raise ValueError("run steps target pace or HR, not power")
        if self.sport == "gym" and self.steps:
            raise ValueError("gym sessions have no steps")
        return self

    def total_s(self) -> int:
        if not self.steps:
            return self.duration_s or 0
        total = 0
        for item in self.steps:
            if isinstance(item, Repeat):
                total += item.times * sum(s.duration_s for s in item.steps)
            else:
                total += item.duration_s
        return total


def _flat(items: list[Step | Repeat]) -> list[Step]:
    out: list[Step] = []
    for item in items:
        out.extend(item.steps if isinstance(item, Repeat) else [item])
    return out
