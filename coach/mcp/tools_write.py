"""coach-mcp write tools (U10): thin wrappers over coach.store, actor "claude".

Every write goes through the store (audit + undo). Logging tools are idempotent on
``client_ref``. Plan tools change planned sessions, then run a reconcile against
intervals.icu and report ``sync``: "synced" when the touched rows are clean, else
"pending" (the DB change stays; the next reconcile pushes it). Tools take values,
never photos: Claude reads the photo, states its confidence and asks Lapo when unsure.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from coach.integrations.intervals import IntervalsError
from coach.mcp.format import Clock, Result, error, utc_now, written
from coach.plan import month as month_plan
from coach.plan.reconcile import EventsClient, reconcile
from coach.store import logs, plan_ops
from coach.store.db import connect_write, fetch_all, fetch_one, today_local

ACTOR = "claude"
LOGGING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PLAN = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
_SYNC_ERRORS = (IntervalsError, httpx.HTTPError, sqlite3.Error)

ClientFactory = Callable[[], EventsClient]
Today = Callable[[], date]


def _kcal_total(conn: sqlite3.Connection, day: str) -> int:
    row = fetch_one(conn, "SELECT coalesce(sum(kcal), 0) AS total FROM meal WHERE local_date = ?", (day,))
    return int(row["total"]) if row else 0


def _pending(detail: str) -> Result:
    return {"status": "pending", "detail": f"{detail}; the change is saved and syncs on the next reconcile"}


def sync_sessions(conn: sqlite3.Connection, factory: ClientFactory | None, now: Clock,
                  session_ids: Iterable[int]) -> Result:
    """Reconcile with intervals.icu, then report whether the touched sessions are clean."""
    if factory is None:
        return _pending("intervals.icu is not configured")
    try:
        client = factory()
    except _SYNC_ERRORS as exc:
        return _pending(f"intervals.icu unavailable ({exc})")
    try:
        summary = reconcile(conn, client, now=now())
    except _SYNC_ERRORS as exc:
        return _pending(f"reconcile failed ({exc})")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    ids = sorted(set(session_ids))
    dirty = [r["external_id"] for r in fetch_all(
        conn, f"SELECT external_id FROM planned_session WHERE sync_dirty = 1 AND id IN ({','.join('?' * len(ids))})",
        tuple(ids))] if ids else []
    if dirty or summary["errors"]:
        return {**_pending("intervals.icu did not accept every change"), "dirty": dirty,
                "errors": summary["errors"][:3]}
    return {"status": "synced"}


def _label(row: Result | None) -> str:
    return f"{row['name']} ({row['external_id']})" if row else "session"


def register(mcp: FastMCP, db_path: Path | str, *, client_factory: ClientFactory | None = None,
             today: Today = today_local, now: Clock = utc_now) -> None:
    """Add the write tools to `mcp`. `client_factory`/`today`/`now` are injectable for tests."""

    @contextmanager
    def db() -> Iterator[sqlite3.Connection]:
        conn = connect_write(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def plan_result(conn: sqlite3.Connection, res: plan_ops.OpResult, done: str, client_ref: str | None) -> Result:
        if res.status != "ok":
            return written(f"Not changed: {res.message}", status=res.status, free_slots=res.free_slots,
                           client_ref=client_ref)
        rows = res.rows or ([res.row] if res.row else [])
        sync = sync_sessions(conn, client_factory, now, [int(r["id"]) for r in rows])
        return written(f"{done}; intervals.icu {sync['status']}", status="ok", sessions=rows, sync=sync,
                       audit_id=res.audit_id, client_ref=client_ref)

    # --- logging -------------------------------------------------------------

    @mcp.tool(annotations=LOGGING)
    def log_meal(description: str, kcal: int | None = None, carbs_g: float | None = None,
                 protein_g: float | None = None, fat_g: float | None = None,
                 source: Literal["photo", "text"] = "photo", confidence: float | None = None,
                 confirmed: bool = False, local_date: str | None = None, client_ref: str | None = None) -> Result:
        """Log a meal from values you read off Lapo's photo or message: estimate kcal and macros, set confidence 0-1, and ask Lapo before logging when you are unsure."""
        with db() as conn:
            try:
                res = logs.log_meal(conn, description=description, kcal=kcal, carbs_g=carbs_g, protein_g=protein_g,
                                    fat_g=fat_g, source=source, confidence=confidence, confirmed=confirmed,
                                    local_date=local_date, client_ref=client_ref, actor=ACTOR)
            except ValueError as exc:
                return error(str(exc))
            assert res.row is not None
            day = str(res.row["local_date"])
            total = _kcal_total(conn, day)
        what = "Already logged" if res.status == "existing" else "Logged"
        kcal_txt = f"{res.row['kcal']} kcal" if res.row["kcal"] is not None else "kcal unknown"
        return written(f"{what} {res.row['description']} ({kcal_txt}); {day} total {total} kcal",
                       status=res.status, meal=res.row, day_kcal_total=total, audit_id=res.audit_id)

    @mcp.tool(annotations=LOGGING)
    def log_weight(kg: float, local_date: str | None = None, confirm: bool = False,
                   client_ref: str | None = None) -> Result:
        """Log Lapo's body weight in kg read off a scale photo or message; if the reply says needs_confirmation, ask Lapo and call again with confirm=true."""
        with db() as conn:
            try:
                res = logs.log_weight(conn, kg=kg, local_date=local_date, confirm=confirm, client_ref=client_ref,
                                      actor=ACTOR)
            except ValueError as exc:
                return error(str(exc))
            day = str(res.row["date"]) if res.row else (local_date or today().isoformat())
            mean = logs.weight_mean_7d(conn, day)
        mean_txt = f"; 7-day mean {mean:.1f} kg" if mean is not None else ""
        if res.status == "needs_confirmation":
            return written(f"{kg} kg looks implausible ({', '.join(res.flags[1:])}); ask Lapo, then confirm",
                           status=res.status, flags=res.flags, mean_7d=mean)
        assert res.row is not None
        what = {"existing": "Already logged", "replaced": "Updated"}.get(res.status, "Logged")
        extra = f" ({res.message})" if res.status == "replaced" else ""
        return written(f"{what} {res.row['kg']} kg for {day}{extra}{mean_txt}", status=res.status,
                       weight=res.row, previous=res.previous, flags=res.flags, mean_7d=mean, audit_id=res.audit_id)

    @mcp.tool(annotations=LOGGING)
    def add_note(text: str, tags: list[str] | None = None, local_date: str | None = None,
                 client_ref: str | None = None) -> Result:
        """Save a free-text note from Lapo (how a session felt, soreness, travel, illness) with optional tags."""
        with db() as conn:
            try:
                res = logs.add_note(conn, text=text, tags=tags, local_date=local_date, client_ref=client_ref,
                                    actor=ACTOR)
            except ValueError as exc:
                return error(str(exc))
        assert res.row is not None
        what = "Note already saved" if res.status == "existing" else "Note saved"
        return written(f"{what} for {res.row['local_date']}", status=res.status, note=res.row, audit_id=res.audit_id)

    @mcp.tool(annotations=LOGGING)
    def log_supplement(supplement: str, local_date: str | None = None, timing: str | None = None,
                       client_ref: str | None = None) -> Result:
        """Record that Lapo took a supplement (by name or id) on a day."""
        ref: int | str = int(supplement) if supplement.isdigit() else supplement
        with db() as conn:
            try:
                res = logs.log_supplement(conn, supplement=ref, local_date=local_date, timing=timing,
                                          client_ref=client_ref, actor=ACTOR)
            except ValueError as exc:
                return error(str(exc))
        if res.row is None:
            return error(res.message or f"no supplement {supplement!r}; add it with set_supplement first")
        what = "Already recorded" if res.status == "existing" else "Recorded"
        return written(f"{what} {supplement} on {res.row['local_date']}", status=res.status, intake=res.row,
                       audit_id=res.audit_id)

    @mcp.tool(annotations=LOGGING)
    def set_supplement(name: str, timing: Literal["morning", "lunch", "evening", "with_meal"],
                       dose: str | None = None, active: bool = True, notes: str | None = None,
                       supplement_id: int | None = None, client_ref: str | None = None) -> Result:
        """Add a supplement to Lapo's list or change its dose, timing or active flag."""
        with db() as conn:
            try:
                res = logs.upsert_supplement(conn, name=name, timing=timing, dose=dose, active=active, notes=notes,
                                             supplement_id=supplement_id, actor=ACTOR)
            except ValueError as exc:
                return error(str(exc))
        if res.row is None:
            return error(res.message or "supplement not saved")
        what = {"created": "Added", "existing": "Unchanged"}.get(res.status, "Updated")
        state = "" if res.row["active"] else " (inactive)"
        return written(f"{what} {res.row['name']} {res.row['dose'] or ''} {res.row['timing']}{state}".replace("  ", " "),
                       status=res.status, supplement=res.row, audit_id=res.audit_id, client_ref=client_ref)

    @mcp.tool(annotations=LOGGING)
    def save_weekly_review(week_start: str, text: str, client_ref: str | None = None) -> Result:
        """Save your weekly review text for the week starting on a Monday (YYYY-MM-DD); saving again replaces it."""
        with db() as conn:
            try:
                res = logs.save_weekly_review(conn, week_start=week_start, text=text, actor=ACTOR)
            except ValueError as exc:
                return error(str(exc))
        what = {"existing": "Review unchanged", "replaced": "Review replaced"}.get(res.status, "Review saved")
        return written(f"{what} for week of {week_start}", status=res.status, audit_id=res.audit_id,
                       client_ref=client_ref)

    # --- plan ----------------------------------------------------------------

    @mcp.tool(annotations=PLAN)
    def move_session(external_id: str, new_date: str, new_slot: Literal["am", "pm"] | None = None,
                     lock: bool = False, reason: str | None = None, client_ref: str | None = None) -> Result:
        """Move a planned session to another day/slot and push it to intervals.icu; set lock=true only when Lapo says it must not change again (e.g. "keep it hard")."""
        with db() as conn:
            try:
                res = plan_ops.move_session(conn, external_id, new_date, new_slot, lock=lock, actor=ACTOR,
                                            reason=reason, today=today())
            except ValueError as exc:
                return error(str(exc))
            row = res.row
            done = f"Moved {_label(row)} to {row['date']} {row['slot']}" if row else "Moved"
            return plan_result(conn, res, done + (" and locked it" if lock else ""), client_ref)

    @mcp.tool(annotations=PLAN)
    def skip_session(external_id: str, reason: str | None = None, client_ref: str | None = None) -> Result:
        """Mark a planned session as skipped and remove it from intervals.icu."""
        with db() as conn:
            res = plan_ops.skip_session(conn, external_id, actor=ACTOR, reason=reason)
            return plan_result(conn, res, f"Skipped {_label(res.row)}", client_ref)

    @mcp.tool(annotations=PLAN)
    def swap_sessions(external_id_a: str, external_id_b: str, reason: str | None = None,
                      client_ref: str | None = None) -> Result:
        """Swap the days/slots of two planned sessions and push both to intervals.icu."""
        with db() as conn:
            res = plan_ops.swap_sessions(conn, external_id_a, external_id_b, actor=ACTOR, reason=reason,
                                         today=today())
            return plan_result(conn, res, f"Swapped {external_id_a} and {external_id_b}", client_ref)

    @mcp.tool(annotations=PLAN)
    def save_month_plan(month: str, rationale: str, sessions: list[dict[str, Any]],
                        client_ref: str | None = None) -> Result:
        """Save a month plan (YYYY-MM) replacing its future sessions; each session has date, slot (am/pm), sport, kind, name, duration_min and steps_text in intervals.icu text, e.g. "Warmup\\n- 15m 55%\\n\\nMain set 3x\\n- 12m 95-100% 90rpm\\n- 5m 55%\\n\\nCooldown\\n- 10m 50%", "- 2h Z2 HR" or "- 45m 4:45/km Pace"."""
        with db() as conn:
            try:
                saved = month_plan.save_month_plan(conn, month, rationale, sessions, actor=ACTOR, today=today())
            except month_plan.MonthPlanError as exc:
                return {"error": f"month plan rejected ({len(exc.problems)} problem(s)); fix and resend",
                        "problems": exc.problems}
            first = f"{month}-01"
            rows = fetch_all(conn, "SELECT id FROM planned_session WHERE date >= ? AND substr(date, 1, 7) = ?",
                             (max(first, today().isoformat()), month))
            sync = sync_sessions(conn, client_factory, now, [int(r["id"]) for r in rows])
        warnings: list[str] = saved["warnings"]
        warn_txt = f"; warnings: {'; '.join(warnings)}" if warnings else ""
        return written(f"Saved {saved['saved']} sessions for {month}{warn_txt}; intervals.icu {sync['status']}",
                       status="ok", saved=saved["saved"], replaced=saved["replaced"], warnings=warnings, sync=sync,
                       client_ref=client_ref)

    @mcp.tool(annotations=PLAN)
    def undo_change(audit_id: int, client_ref: str | None = None) -> Result:
        """Undo one earlier change by its audit_id (from a write tool's reply) and re-sync intervals.icu if it touched the plan."""
        with db() as conn:
            res = plan_ops.undo(conn, audit_id, actor=ACTOR)
            if res.status != "undone":
                return written(f"Not undone: {res.message}", status=res.status, client_ref=client_ref)
            sessions = [r for r in res.rows if "external_id" in r]
            sync = sync_sessions(conn, client_factory, now, [int(r["id"]) for r in sessions]) if sessions else None
        tail = f"; intervals.icu {sync['status']}" if sync else ""
        return written(f"Undid change {audit_id}{tail}", status="undone", audit_id=res.audit_id, rows=res.rows,
                       sync=sync, client_ref=client_ref)
