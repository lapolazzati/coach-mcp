"""Session -> intervals.icu workout text and event payload.

intervals.icu parses its own text format and syncs the result to Zwift and
Garmin, so this text is the single delivery format:

    - 10m 55-65%
    Main set 3x
    - 8m 88-92%
    - 4m 55%
"""

from __future__ import annotations

from coach.workouts.model import Repeat, Session, Step

ICU_TYPE = {"ride": "VirtualRide", "run": "Run", "gym": "WeightTraining"}


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s" if s else ""]
    return "".join(parts) or "0s"


def fmt_pace(s_per_km: int) -> str:
    m, s = divmod(s_per_km, 60)
    return f"{m}:{s:02d}/km Pace"


def step_line(step: Step) -> str:
    parts = ["-"]
    if step.label:
        parts.append(step.label)
    parts.append(fmt_duration(step.duration_s))
    if step.power_lo is not None:
        hi = step.power_hi if step.power_hi is not None else step.power_lo
        if step.ramp:
            parts.append(f"ramp {step.power_lo}-{hi}%")
        elif hi != step.power_lo:
            parts.append(f"{step.power_lo}-{hi}%")
        else:
            parts.append(f"{step.power_lo}%")
    elif step.hr_zone is not None:
        parts.append(f"Z{step.hr_zone} HR")
    elif step.pace_s_per_km is not None:
        parts.append(fmt_pace(step.pace_s_per_km))
    if step.cadence:
        parts.append(f"{step.cadence}rpm")
    return " ".join(parts)


def workout_text(session: Session) -> str:
    blocks: list[str] = []
    for item in session.steps:
        if isinstance(item, Repeat):
            header = f"{item.label} {item.times}x" if item.label else f"{item.times}x"
            blocks.append("\n".join([header, *(step_line(s) for s in item.steps)]))
        else:
            blocks.append(step_line(item))
    body = "\n\n".join(blocks)
    if session.rationale:
        body = f"{session.rationale}\n\n{body}" if body else session.rationale
    return body


def event_payload(session: Session) -> dict:
    payload = {
        "category": "WORKOUT",
        "external_id": session.external_id,
        "start_date_local": session.start.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": ICU_TYPE[session.sport],
        "name": session.name,
        "description": workout_text(session),
        "indoor": session.indoor,
    }
    if session.sport == "gym" or not session.steps:
        payload["moving_time"] = session.total_s()
    return payload
