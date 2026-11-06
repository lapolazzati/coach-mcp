"""Morning readiness job (U7, KTD9).

Runs every 10 minutes from 05:30 to 17:50. Each run pulls today's wellness,
scores readiness and, on amber or red, eases today's hard sessions whose
decision deadline is still ahead. A session's deadline is 10 minutes before
its start, and 06:50 at the latest for a session starting before 07:30, so
an 18:00 session can still be eased when sleep data lands at 07:40.

Every write goes through store functions (plan_ops.ease_session audits it
and keeps the original for undo). The day is marked done in sync_state once
no deadline is left ahead, so later runs and restarts become no-ops.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from coach import readiness
from coach.ingest import intervals_sync
from coach.plan import month, reconcile
from coach.store import plan_ops, sync
from coach.store.db import TZ, Row, fetch_all
from coach.workouts.builder import step_line
from coach.workouts.model import Step

log = logging.getLogger(__name__)

ACTOR = "job:morning"
HARD_KINDS = frozenset({"vo2", "threshold", "intervals", "tempo", "sweetspot", "race"})
LEAD = timedelta(minutes=10)
EARLY_START = time(7, 30)
EARLY_DEADLINE = time(6, 50)
EASY_MIN = 45
EASY_NAME = "45 min Z2"


def state_key(day: date) -> str:
    return f"morning:{day.isoformat()}"


def _advice_key(day: date, external_id: str) -> str:
    return f"morning:{day.isoformat()}:advice:{external_id}"


def local_now(now: datetime) -> datetime:
    return now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)


def deadline(row: Row) -> datetime:
    """Last moment the job may still change this session (KTD9)."""
    start = reconcile.start_of(row).replace(tzinfo=TZ)
    cutoff = start - LEAD
    if start.time() < EARLY_START:
        cutoff = min(cutoff, datetime.combine(start.date(), EARLY_DEADLINE, TZ))
    return cutoff


def is_run(sport: Any) -> bool:
    try:
        return month.SPORTS[month.canonical_sport(str(sport))][1] == "run"
    except ValueError:
        return "run" in str(sport).lower()


def eased_content(sport: Any) -> dict[str, Any]:
    if is_run(sport):
        step = Step(duration_s=EASY_MIN * 60, hr_zone=2)
    else:
        step = Step(duration_s=EASY_MIN * 60, power_lo=60, power_hi=70)
    return {
        "kind": "endurance",
        "name": EASY_NAME,
        "duration_min": EASY_MIN,
        "session_json": {"steps": [step.model_dump(mode="json", exclude_none=True)], "steps_text": step_line(step)},
    }


def _hm(secs: float) -> str:
    h, m = divmod(round(secs / 60), 60)
    return f"{h}h{m:02d}"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def ease_reason(score: readiness.Readiness, row: Row) -> str:
    """Human reason from the readiness signals, e.g. 'Sleep 4h50 (<5h30), HRV 38 vs baseline 52±6 → ...'."""
    sig = score.signals
    flags = set(score.flags)
    parts: list[str] = []
    sleep, hrv, rhr, bb = (_num(sig.get(k)) for k in ("sleep", "hrv", "rhr", "body_battery"))
    if "sleep_short" in flags and sleep is not None:
        parts.append(f"Sleep {_hm(sleep)} (<{_hm(readiness.SLEEP_MIN_SECS)})")
    if "hrv_low" in flags and hrv is not None:
        base = sig.get("hrv_baseline") or {}
        parts.append(f"HRV {hrv:.0f} vs baseline {base.get('mean', 0):.0f}±{base.get('sd', 0):.0f}")
    if "rhr_high" in flags and rhr is not None:
        base = sig.get("rhr_baseline") or {}
        parts.append(f"RHR {rhr:.0f} vs baseline {base.get('mean', 0):.0f}")
    if "body_battery_low" in flags and bb is not None:
        parts.append(f"Body Battery {bb:.0f} (<{readiness.BODY_BATTERY_MIN})")
    why = ", ".join(parts) or f"readiness {score.status}"
    return f"{why} → swapped {row.get('name') or row.get('kind')} for {EASY_NAME}"


def _candidates(conn: sqlite3.Connection, day: date) -> list[Row]:
    kinds = sorted(HARD_KINDS)
    return fetch_all(
        conn,
        "SELECT * FROM planned_session WHERE date = ? AND status = 'planned'"
        f" AND lower(coalesce(kind, '')) IN ({', '.join('?' * len(kinds))})"
        " AND coalesce(eased_from_json, '') = '' ORDER BY coalesce(start_time, slot)",
        (day.isoformat(), *kinds),
    )


def run(conn: sqlite3.Connection, client: Any, now: datetime, *, dry_run: bool = False) -> dict[str, Any]:
    """One morning pass. dry_run scores and returns proposals without writing anything."""
    now = local_now(now)
    today = now.date()
    result: dict[str, Any] = {"date": today.isoformat(), "dry_run": dry_run, "synced": None, "status": None,
                              "eased": [], "advice": [], "proposals": [], "too_late": [], "reconcile": None,
                              "done": False}
    if client is not None and not dry_run:
        result["synced"] = intervals_sync.sync_wellness(client, conn, today, today)
    if dry_run:
        score = readiness.score(today, *readiness.load(conn, today))
    else:
        score, _ = readiness.compute_and_store(conn, today)
    result["status"] = score.status

    pending = []
    for row in _candidates(conn, today):
        ext = str(row["external_id"])
        if sync.get_state(conn, _advice_key(today, ext)):
            continue
        if now >= deadline(row):
            result["too_late"].append(ext)
            continue
        if score.status not in ("amber", "red"):
            pending.append(row)
            continue
        eased = eased_content(row.get("sport"))
        reason = ease_reason(score, row)
        if dry_run:
            result["proposals"].append({"external_id": ext, "eased": eased, "reason": reason})
            pending.append(row)
            continue
        op = plan_ops.ease_session(conn, ext, eased=eased, reason=reason, actor=ACTOR)
        if op.status == "ok":
            result["eased"].append(ext)
        elif op.status == "advice":
            result["advice"].append(ext)
            sync.set_state(conn, _advice_key(today, ext), "advice")
        else:
            log.warning("morning: ease %s %s: %s", ext, op.status, op.message)

    if dry_run:
        return result
    if client is not None:
        result["reconcile"] = reconcile.reconcile(conn, client, now=now)
    if not any(deadline(r) > now for r in pending):
        sync.set_state(conn, state_key(today), "done")
        result["done"] = True
    return result
