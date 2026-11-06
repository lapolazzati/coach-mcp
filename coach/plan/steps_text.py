"""intervals.icu workout text -> Session steps, the inverse of builder.workout_text().

Grammar (one item per line; blank lines separate blocks):

    - [label] DURATION [TARGET] [CADENCE]     a step
    [label] Nx                                 a repeat header; the step lines
                                               right below it (no blank line)
                                               are the repeated set
    Warmup | Cooldown | Main set               a section header; labels the
                                               unlabelled steps below it

    DURATION  1h, 10m, 30s, 1h30m, 2m30s
    TARGET    55%  88-92%  ramp 50-75%  (optional "FTP" after the %),
              Z2 HR, 4:45/km Pace
    CADENCE   90rpm

Anything else is an error that names the line, so Claude can fix its text.
"""

from __future__ import annotations

import json
import re

from pydantic import TypeAdapter, ValidationError

from coach.workouts.model import Repeat, Step

_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_POWER = re.compile(r"^(\d+)(?:-(\d+))?%$")
_ZONE = re.compile(r"^Z([1-9])$", re.IGNORECASE)
_PACE = re.compile(r"^(\d+):([0-5]\d)/km$")
_CADENCE = re.compile(r"^(\d+)rpm$", re.IGNORECASE)
_REPEAT = re.compile(r"^(?:(.+?)\s+)?(\d+)x$", re.IGNORECASE)
SECTION_HEADERS = {"warmup": "Warmup", "warm up": "Warmup", "cooldown": "Cooldown", "cool down": "Cooldown",
                   "main set": "Main set"}


class StepsTextError(ValueError):
    def __init__(self, line_no: int, line: str, reason: str):
        self.line_no = line_no
        self.line = line
        self.reason = reason
        super().__init__(f"line {line_no}: {line!r}: {reason}")


def _duration(token: str) -> int | None:
    m = _DURATION.match(token)
    if not m or not any(m.groups()):
        return None
    h, mins, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mins * 60 + s


def parse_step(line: str, line_no: int = 1) -> Step:
    """One '- ...' line -> Step."""
    def fail(reason: str) -> StepsTextError:
        return StepsTextError(line_no, line, reason)

    body = line.strip()
    if not body.startswith("-"):
        raise fail("a step line starts with '-'")
    tokens = body[1:].split()
    dur_at = next((i for i, t in enumerate(tokens) if _duration(t) is not None), None)
    if dur_at is None:
        raise fail("no duration (use e.g. 10m, 1h30m, 30s)")
    label = " ".join(tokens[:dur_at]) or None
    duration_s = _duration(tokens[dur_at])
    assert duration_s is not None
    if duration_s <= 0:
        raise fail("duration must be above zero")
    fields: dict[str, object] = {"duration_s": duration_s, "label": label}
    rest = tokens[dur_at + 1:]
    i = 0
    target_set = False
    while i < len(rest):
        tok = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if tok.lower() == "ramp" and not target_set and _POWER.match(nxt):
            m = _POWER.match(nxt)
            assert m is not None
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            fields.update(power_lo=lo, power_hi=hi, ramp=True)
            target_set, i = True, i + 2
        elif (m := _POWER.match(tok)) and not target_set:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            if hi < lo:
                raise fail(f"power range {lo}-{hi}% goes down; write it low-high or use 'ramp'")
            fields.update(power_lo=lo, power_hi=None if hi == lo else hi)
            target_set, i = True, i + 1
        elif (m := _ZONE.match(tok)) and nxt.upper() == "HR" and not target_set:
            zone = int(m.group(1))
            if zone > 5:
                raise fail(f"HR zone Z{zone} is out of range Z1-Z5")
            fields["hr_zone"] = zone
            target_set, i = True, i + 2
        elif (m := _PACE.match(tok)) and nxt.lower() == "pace" and not target_set:
            fields["pace_s_per_km"] = int(m.group(1)) * 60 + int(m.group(2))
            target_set, i = True, i + 2
        elif (m := _CADENCE.match(tok)) and "cadence" not in fields:
            fields["cadence"] = int(m.group(1))
            i += 1
        elif tok.upper() == "FTP" and "power_lo" in fields and rest[i - 1].endswith("%"):
            i += 1
        else:
            raise fail(f"don't understand {tok!r} (targets: 55%, 88-92%, ramp 50-75%, Z2 HR, 4:45/km Pace, 90rpm)")
    try:
        return Step(**fields)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise fail(exc.errors()[0]["msg"]) from exc


def parse(text: str) -> list[Step | Repeat]:
    """Workout text -> steps. Raises StepsTextError naming the first bad line."""
    items: list[Step | Repeat] = []
    repeat: Repeat | None = None
    repeat_line: tuple[int, str] | None = None
    section: str | None = None

    def close_repeat() -> None:
        nonlocal repeat, repeat_line
        if repeat is not None:
            if not repeat.steps:
                assert repeat_line is not None
                raise StepsTextError(*repeat_line, "a repeat header needs step lines right below it")
            items.append(repeat)
        repeat, repeat_line = None, None

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            close_repeat()
            section = None
            continue
        if line.startswith("-"):
            step = parse_step(line, n)
            if repeat is not None:
                repeat.steps.append(step)
            else:
                if section and step.label is None:
                    step.label = section
                items.append(step)
            continue
        close_repeat()
        if (m := _REPEAT.match(line)) is not None:
            times = int(m.group(2))
            if times < 2:
                raise StepsTextError(n, line, "a repeat needs at least 2x")
            repeat = Repeat.model_construct(times=times, steps=[], label=m.group(1))
            repeat_line = (n, line)
            continue
        if line.lower() in SECTION_HEADERS:
            section = SECTION_HEADERS[line.lower()]
            continue
        raise StepsTextError(n, line, "not a step ('- 10m 55%'), a repeat header ('3x') or a section header")
    close_repeat()
    # Re-validate repeats built incrementally.
    return [Repeat(times=i.times, steps=i.steps, label=i.label) if isinstance(i, Repeat) else i for i in items]


_STEPS: TypeAdapter[list[Step | Repeat]] = TypeAdapter(list[Step | Repeat])


def stored_steps(session_json: str | None) -> list[Step | Repeat]:
    """Steps from a planned_session.session_json: model dicts, else steps_text, else legacy
    text lines. Raises ValueError when present but unreadable."""
    data = json.loads(session_json or "{}")
    if not isinstance(data, dict):
        raise ValueError("session_json is not an object")  # noqa: TRY004 - callers catch ValueError
    steps = data.get("steps")
    if isinstance(steps, list) and steps and all(isinstance(s, dict) for s in steps):
        return _STEPS.validate_python(steps)
    if isinstance(data.get("steps_text"), str):
        return parse(data["steps_text"])
    if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
        return parse("\n".join(steps))
    return []
