"""Plan writes: move, skip, swap, ease, undo (KTD9, KTD10).

Every op runs in one transaction with its audit row, sets sync_dirty and bumps
row_version. The intervals.icu push happens after commit (U5) and is driven by
sync_dirty plus the tombstones written here.

Lock semantics:
- pinned_date is set by any move or swap; the reconciler must not move the
  session back. Pinned sessions can still be eased.
- user_locked is set only on an explicit lock=True. A job never moves a locked
  session, and a job ease on a locked session records advice only.
- A "claude" actor (Lapo asked) can move and ease locked sessions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from coach.store import audit, sync
from coach.store.audit import UndoResult
from coach.store.db import Row, fetch_one, now_utc, parse_date, today_local, transaction

FREE_SLOT_LOOKAHEAD_DAYS = 3
EASE_FIELDS = ("session_json", "kind", "name", "duration_min", "sport")
UNMOVABLE_STATUSES = ("done",)


@dataclass
class OpResult:
    status: str  # ok | refused | advice | not_found
    message: str = ""
    row: Row | None = None
    rows: list[Row] = field(default_factory=list)
    audit_id: int | None = None
    free_slots: list[dict[str, str]] = field(default_factory=list)


def get_session(conn: sqlite3.Connection, external_id: str) -> Row | None:
    return fetch_one(conn, "SELECT * FROM planned_session WHERE external_id = ?", (external_id,))


def _reload(conn: sqlite3.Connection, session_id: int) -> Row:
    row = fetch_one(conn, "SELECT * FROM planned_session WHERE id = ?", (session_id,))
    assert row is not None
    return row


def free_slots(conn: sqlite3.Connection, start: date, days: int = FREE_SLOT_LOOKAHEAD_DAYS + 1,
               exclude_ids: tuple[int, ...] = ()) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        taken = sync.taken_slots(conn, day, exclude_ids)
        out.extend({"date": day, "slot": slot} for slot in sync.SLOTS if slot not in taken)
    return out


def move_session(
    conn: sqlite3.Connection,
    external_id: str,
    new_date: str | date,
    new_slot: str | None = None,
    *,
    lock: bool = False,
    actor: str = "claude",
    reason: str | None = None,
    today: date | None = None,
) -> OpResult:
    audit.check_actor(actor)
    target_day = parse_date(new_date)
    if target_day < (today or today_local()):
        return OpResult("refused", f"{target_day.isoformat()} is in the past")
    with transaction(conn):
        row = get_session(conn, external_id)
        if row is None:
            return OpResult("not_found", f"no session {external_id}")
        if row["status"] in UNMOVABLE_STATUSES:
            return OpResult("refused", f"{external_id} is already {row['status']}", row)
        if row["user_locked"] and audit.is_job(actor):
            return OpResult("refused", f"{external_id} is locked by Lapo; jobs don't move it", row)
        slot = new_slot or row["slot"]
        new_id = sync.external_id(target_day.isoformat(), slot)
        if new_id == external_id:
            return OpResult("refused", f"{external_id} is already on {target_day.isoformat()} {slot}", row)
        holder = get_session(conn, new_id)
        if holder is not None:
            return OpResult(
                "refused", f"{target_day.isoformat()} {slot} is taken by {holder['name']}", row,
                free_slots=free_slots(conn, target_day, exclude_ids=(int(row["id"]),)),
            )
        sid = int(row["id"])
        sync.reassign_external_ids(conn, [(sid, external_id, new_id)], reason="moved")
        conn.execute(
            "UPDATE planned_session SET date = ?, slot = ?, start_time = ?, pinned_date = 1,"
            " user_locked = MAX(user_locked, ?) WHERE id = ?",
            (target_day.isoformat(), slot, row["start_time"] if slot == row["slot"] else None, int(lock), sid),
        )
        sync.mark_dirty(conn, sid)
        after = _reload(conn, sid)
        aid = audit.record(conn, actor=actor, action="move_session", entity="planned_session", entity_id=sid,
                           before=row, after=after, reason=reason or f"moved from {external_id}")
    return OpResult("ok", f"moved {external_id} -> {new_id}", after, [after], aid)


def skip_session(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    actor: str = "claude",
    reason: str | None = None,
) -> OpResult:
    """Mark a session skipped. The row and its external_id stay; the push (U5) removes the remote event."""
    audit.check_actor(actor)
    with transaction(conn):
        row = get_session(conn, external_id)
        if row is None:
            return OpResult("not_found", f"no session {external_id}")
        if row["status"] in ("skipped", "done"):
            return OpResult("refused", f"{external_id} is already {row['status']}", row)
        if row["user_locked"] and audit.is_job(actor):
            return OpResult("refused", f"{external_id} is locked by Lapo; jobs don't skip it", row)
        sid = int(row["id"])
        conn.execute("UPDATE planned_session SET status = 'skipped' WHERE id = ?", (sid,))
        sync.mark_dirty(conn, sid)
        after = _reload(conn, sid)
        aid = audit.record(conn, actor=actor, action="skip_session", entity="planned_session", entity_id=sid,
                           before=row, after=after, reason=reason)
    return OpResult("ok", f"skipped {external_id}", after, [after], aid)


def swap_sessions(
    conn: sqlite3.Connection,
    external_id_a: str,
    external_id_b: str,
    *,
    actor: str = "claude",
    reason: str | None = None,
    today: date | None = None,
) -> OpResult:
    """Exchange the date and slot of two sessions. Both end pinned."""
    audit.check_actor(actor)
    if external_id_a == external_id_b:
        return OpResult("refused", "cannot swap a session with itself")
    with transaction(conn):
        a, b = get_session(conn, external_id_a), get_session(conn, external_id_b)
        missing = [e for e, r in ((external_id_a, a), (external_id_b, b)) if r is None]
        if a is None or b is None:
            return OpResult("not_found", f"no session {', '.join(missing)}")
        first_day = min(parse_date(a["date"]), parse_date(b["date"]))
        if first_day < (today or today_local()):
            return OpResult("refused", "cannot swap a session in the past")
        for r in (a, b):
            if r["status"] in UNMOVABLE_STATUSES:
                return OpResult("refused", f"{r['external_id']} is already {r['status']}")
            if r["user_locked"] and audit.is_job(actor):
                return OpResult("refused", f"{r['external_id']} is locked by Lapo; jobs don't move it")
        aid_, bid = int(a["id"]), int(b["id"])
        sync.reassign_external_ids(conn, [(aid_, a["external_id"], b["external_id"]),
                                          (bid, b["external_id"], a["external_id"])], reason="swapped")
        for me, other in ((a, b), (b, a)):
            conn.execute(
                "UPDATE planned_session SET date = ?, slot = ?, start_time = ?, pinned_date = 1 WHERE id = ?",
                (other["date"], other["slot"], other["start_time"], me["id"]),
            )
            sync.mark_dirty(conn, int(me["id"]))
        after = [_reload(conn, aid_), _reload(conn, bid)]
        aid = audit.record(conn, actor=actor, action="swap_sessions", entity="planned_session",
                           entity_id=f"{aid_},{bid}", before=[a, b], after=after, reason=reason)
    return OpResult("ok", f"swapped {external_id_a} <-> {external_id_b}", None, after, aid)


def ease_session(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    eased: dict[str, Any],
    reason: str,
    actor: str,
) -> OpResult:
    """Replace a session's content with an easier one (computed by the caller, U7).

    `eased` holds any of session_json (dict or JSON str), kind, name,
    duration_min, sport. The original content goes to eased_from_json, once:
    easing twice keeps the first original. A job ease on a user_locked session
    records advice only.
    """
    audit.check_actor(actor)
    unknown = set(eased) - set(EASE_FIELDS)
    if unknown or not eased:
        raise ValueError(f"eased must hold some of {EASE_FIELDS}; got {sorted(eased)}")
    values = dict(eased)
    if "session_json" in values and not isinstance(values["session_json"], str):
        values["session_json"] = json.dumps(values["session_json"], sort_keys=True)
    with transaction(conn):
        row = get_session(conn, external_id)
        if row is None:
            return OpResult("not_found", f"no session {external_id}")
        if row["status"] != "planned":
            return OpResult("refused", f"{external_id} is {row['status']}; nothing to ease", row)
        sid = int(row["id"])
        if row["user_locked"] and audit.is_job(actor):
            aid = audit.record(conn, actor=actor, action="ease_advice", entity="planned_session", entity_id=sid,
                               before=None, after={"proposed": values}, reason=reason)
            return OpResult("advice", f"{external_id} is locked by Lapo; advice recorded: {reason}", row,
                            [row], aid)
        original = row["eased_from_json"] or json.dumps({k: row[k] for k in EASE_FIELDS}, sort_keys=True)
        values.update(eased_from_json=original, ease_reason=reason, eased_at=now_utc())
        sets, params = sync.as_params(values)
        conn.execute(f"UPDATE planned_session SET {sets} WHERE id = ?", (*params, sid))
        sync.mark_dirty(conn, sid)
        after = _reload(conn, sid)
        aid = audit.record(conn, actor=actor, action="ease_session", entity="planned_session", entity_id=sid,
                           before=row, after=after, reason=reason)
    return OpResult("ok", f"eased {external_id}: {reason}", after, [after], aid)


def undo(conn: sqlite3.Connection, audit_id: int, *, actor: str = "claude") -> UndoResult:
    """Undo any audited write (plan ops and logs alike)."""
    return audit.undo(conn, audit_id, actor=actor)
