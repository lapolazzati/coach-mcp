"""steps_text.parse(): intervals.icu workout text -> Session steps (U5)."""

from __future__ import annotations

from datetime import datetime

import pytest

from coach.plan import steps_text
from coach.plan.steps_text import StepsTextError
from coach.workouts.builder import workout_text
from coach.workouts.model import Repeat, Session, Step

START = datetime(2026, 12, 1, 7, 0)


def _session(sport: str, steps: list[Step | Repeat], **kw: object) -> Session:
    return Session(external_id="coach:2026-12-01:am", sport=sport, start=START, name="x", steps=steps, **kw)  # type: ignore[arg-type]


Z2 = _session("ride", [Step(duration_s=600, power_lo=50, power_hi=65, ramp=True, label="Warmup"),
                       Step(duration_s=5400, power_lo=65, power_hi=72, cadence=90),
                       Step(duration_s=300, power_lo=50, label="Cooldown")])
SWEET_SPOT = _session("ride", [
    Step(duration_s=900, power_lo=50, power_hi=75, ramp=True, label="Warmup"),
    Repeat(times=3, label="Main set", steps=[Step(duration_s=720, power_lo=88, power_hi=93),
                                             Step(duration_s=300, power_lo=55)]),
    Step(duration_s=600, power_lo=50, label="Cooldown"),
])
VO2 = _session("ride", [
    Step(duration_s=1200, power_lo=55, power_hi=70, ramp=True),
    Step(duration_s=30, power_lo=120, cadence=105),
    Repeat(times=5, steps=[Step(duration_s=180, power_lo=110, power_hi=120), Step(duration_s=180, power_lo=50)]),
    Step(duration_s=630, power_lo=50),
])
RUN = _session("run", [Step(duration_s=600, hr_zone=1, label="Warmup"), Step(duration_s=2400, hr_zone=2),
                       Repeat(times=4, steps=[Step(duration_s=90, pace_s_per_km=255), Step(duration_s=90, hr_zone=1)]),
                       Step(duration_s=3725, hr_zone=2)])
GYM = Session(external_id="g", sport="gym", start=START, name="Gym", duration_s=2700)


@pytest.mark.parametrize("session", [Z2, SWEET_SPOT, VO2, RUN, GYM], ids=["z2", "sweet_spot", "vo2", "run", "gym"])
def test_round_trip_is_stable(session: Session) -> None:
    text = workout_text(session)
    parsed = steps_text.parse(text)
    assert parsed == session.steps
    rebuilt = session.model_copy(update={"steps": parsed})
    assert workout_text(rebuilt) == text


def test_sweet_spot_text_shape() -> None:
    text = workout_text(SWEET_SPOT)
    assert "Main set 3x\n- 12m 88-93%" in text
    steps = steps_text.parse(text)
    rep = steps[1]
    assert isinstance(rep, Repeat) and rep.times == 3 and rep.label == "Main set" and len(rep.steps) == 2


def test_section_header_labels_following_steps() -> None:
    steps = steps_text.parse("Warmup\n- 10m ramp 50-75%\n\n3x\n- 5m 105%\n- 5m 55%\n\nCooldown\n- 5m 50%")
    first, rep, last = steps
    assert isinstance(first, Step) and first.label == "Warmup" and first.ramp
    assert isinstance(rep, Repeat) and rep.times == 3
    assert isinstance(last, Step) and last.label == "Cooldown" and last.power_lo == 50


def test_lenient_forms() -> None:
    steps = steps_text.parse("- 1h30m 65-72% FTP 90rpm\n\n-  20s 120%\n\n5X\n- 3m 110%\n- 3m 50%\n")
    a, b, rep = steps
    assert isinstance(a, Step) and (a.duration_s, a.power_lo, a.power_hi, a.cadence) == (5400, 65, 72, 90)
    assert isinstance(b, Step) and b.duration_s == 20 and b.power_lo == 120
    assert isinstance(rep, Repeat) and rep.times == 5


def test_blank_text_is_no_steps() -> None:
    assert steps_text.parse("") == []
    assert steps_text.parse("\n  \n") == []


@pytest.mark.parametrize(
    ("text", "line_no", "bad"),
    [
        ("- 10m 55%\n\n- 20 minutes hard", 3, "- 20 minutes hard"),
        ("- 10m 55%\nsome prose here", 2, "some prose here"),
        ("1x\n- 5m 100%", 1, "1x"),
        ("- 5m 100-90%", 1, "- 5m 100-90%"),
        ("3x\n\n- 5m 100%", 1, "3x"),
        ("- 5m Z7 HR", 1, "- 5m Z7 HR"),
        ("- 0m 50%", 1, "- 0m 50%"),
    ],
)
def test_unparseable_text_names_bad_line(text: str, line_no: int, bad: str) -> None:
    with pytest.raises(StepsTextError) as exc:
        steps_text.parse(text)
    assert exc.value.line_no == line_no
    assert bad in str(exc.value) and f"line {line_no}" in str(exc.value)
