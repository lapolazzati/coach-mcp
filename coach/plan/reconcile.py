"""planned_session -> intervals.icu reconciler (KTD10).

One run:
1. list the coach: events in [today, end of window or the latest dirty row];
2. one bulk delete: pending tombstones, orphan coach: events (no row, no
   tombstone), and events of rows that should not be there (skipped, rest);
3. one bulk upsert: rows that are dirty, whose content hash differs from the
   last write, or whose remote event was edited in intervals.icu (logged, then
   overwritten; the row is never locked for it);
4. missing events: a clean, pushed, planned row absent remotely counts a miss;
   two consecutive misses mean Lapo deleted it, so the session is skipped.

All DB writes go through coach.store (KTD8). A failed call leaves the rows
dirty and the tombstones pending, so the next run retries.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from coach.integrations.intervals import IntervalsError
from coach.plan import month, steps_text
from coach.store import plan_ops, sync
from coach.store.db import TZ, Row, fetch_all
from coach.workouts.builder import event_payload
from coach.workouts.model import Repeat, Session, Step

log = logging.getLogger(__name__)

ACTOR = "job:reconcile"
PREFIX = "coach:"
MISSES_MEAN_DELETED = 2
COMPARED_FIELDS = ("name", "type", "start_date_local", "description")
_API_ERRORS = (IntervalsError, httpx.HTTPError)


class EventsClient(Protocol):
    def list_events(self, oldest: date, newest: date, category: str = ...) -> list[dict[str, Any]]: ...
    def upsert_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def delete_events(self, external_ids: list[str]) -> None: ...


def session_steps(row: Row) -> list[Step | Repeat]:
    """The row's steps, or [] (logged) when session_json can't be read."""
    try:
        return steps_text.stored_steps(row.get("session_json"))
    except ValueError as exc:  # JSON, pydantic and StepsTextError are all ValueErrors
        log.warning("%s: steps unreadable, pushing without structure: %s", row.get("external_id"), exc)
        return []


def start_of(row: Row) -> datetime:
    clock = row.get("start_time") or sync.SLOTS.get(str(row.get("slot")), "07:00")
    return datetime.combine(date.fromisoformat(str(row["date"])), time.fromisoformat(str(clock)))


def build_payload(row: Row) -> dict[str, Any]:
    """The intervals.icu event a row should have. Raises ValueError for an unknown or rest sport."""
    sport = month.canonical_sport(str(row["sport"]))
    icu_type, model_sport, indoor = month.SPORTS[sport]
    if icu_type is None or model_sport is None:
        raise ValueError(f"{sport} sessions are not pushed")
    steps = [] if model_sport == "gym" else session_steps(row)
    duration_s = int(row.get("duration_min") or 0) * 60 or None
    fields: dict[str, Any] = {
        "external_id": row["external_id"], "sport": model_sport, "start": start_of(row),
        "name": row.get("name") or row.get("kind") or "Workout", "duration_s": duration_s, "indoor": indoor,
    }
    try:
        session = Session(steps=steps, **fields)
    except ValidationError as exc:
        log.warning("%s: steps don't fit %s, pushing without structure: %s", row["external_id"], sport, exc)
        session = Session(steps=[], **fields)
    payload = event_payload(session)
    payload["type"] = icu_type
    return payload


def wants_event(row: Row) -> bool:
    if row["status"] in ("skipped", "moved"):
        return False
    try:
        return month.SPORTS[month.canonical_sport(str(row["sport"]))][0] is not None
    except ValueError:
        return True  # surfaces as an error when building the payload


def _norm(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "description":
        return "\n".join(line.rstrip() for line in str(value).strip().splitlines())
    if field == "start_date_local":
        return str(value)[:19]
    return value


def remote_diff(event: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    return [f for f in COMPARED_FIELDS if f in event and _norm(f, event[f]) != _norm(f, payload.get(f))]


def _today(now: datetime | None) -> date:
    if now is None:
        return datetime.now(TZ).date()
    return now.astimezone(TZ).date() if now.tzinfo else now.date()


def reconcile(conn: sqlite3.Connection, client: EventsClient, window_days: int = 14,
              now: datetime | None = None) -> dict[str, Any]:
    today = _today(now)
    end = today + timedelta(days=window_days)
    summary: dict[str, Any] = {"pushed": 0, "deleted": 0, "orphans": 0, "overwritten": 0, "missing": 0,
                               "treated_deleted": [], "errors": []}
    rows = fetch_all(conn, "SELECT * FROM planned_session WHERE date >= ? AND (date <= ? OR sync_dirty = 1)"
                           " ORDER BY date, slot", (today.isoformat(), end.isoformat()))
    newest = max([end, *(date.fromisoformat(str(r["date"])) for r in rows)])
    try:
        listed = client.list_events(today, newest)
    except _API_ERRORS as exc:
        summary["errors"].append(f"list_events: {exc}")
        log.error("reconcile: cannot list intervals.icu events: %s", exc)
        return summary
    remote = {e["external_id"]: e for e in listed
              if isinstance(e.get("external_id"), str) and e["external_id"].startswith(PREFIX)}
    owned = {str(r["external_id"]) for r in fetch_all(conn, "SELECT external_id FROM planned_session")}

    # --- what to delete --------------------------------------------------------
    tombstones: list[Row] = []
    for t in sync.pending_tombstones(conn):
        if t["external_id"] in owned:  # a current row holds the id again: nothing to delete
            sync.confirm_tombstone(conn, t["external_id"], int(t["session_id"]))
        else:
            tombstones.append(t)
    tomb_ids = {str(t["external_id"]) for t in tombstones}
    orphans = sorted(x for x in remote if x not in owned and x not in tomb_ids)
    removals: list[tuple[Row, int, str]] = []  # (row, version, hash) whose remote event must go
    pushes: list[tuple[Row, int, str, dict[str, Any]]] = []

    for row in rows:
        ext, sid = str(row["external_id"]), int(row["id"])
        version, digest = int(row["row_version"]), sync.content_hash(row)
        changed = bool(row["sync_dirty"]) or digest != row["last_written_hash"]
        event = remote.get(ext)
        if not wants_event(row):
            if event is not None:
                removals.append((row, version, digest))
            elif changed:
                sync.mark_pushed(conn, sid, version, digest)
            continue
        try:
            payload = build_payload(row)
        except ValueError as exc:
            summary["errors"].append(f"{ext}: {exc}")
            continue
        if event is None:
            if changed:
                pushes.append((row, version, digest, payload))
            elif row["status"] == "planned":
                _missing(conn, row, summary)
            continue
        if row["missing_polls"]:
            sync.reset_missing_polls(conn, sid)
        if changed:
            pushes.append((row, version, digest, payload))
        elif diff := remote_diff(event, payload):
            log.warning("reconcile: %s was edited in intervals.icu (%s); overwriting with the plan",
                        ext, ", ".join(diff))
            summary["overwritten"] += 1
            pushes.append((row, version, digest, payload))

    # --- one bulk delete -------------------------------------------------------
    delete_ids = sorted(tomb_ids | set(orphans) | {str(r["external_id"]) for r, _, _ in removals})
    if delete_ids:
        try:
            client.delete_events(delete_ids)
        except _API_ERRORS as exc:
            summary["errors"].append(f"delete_events: {exc}")
            log.error("reconcile: bulk delete of %d events failed: %s", len(delete_ids), exc)
        else:
            for t in tombstones:
                sync.confirm_tombstone(conn, t["external_id"], int(t["session_id"]))
            for r, version, digest in removals:
                sync.mark_pushed(conn, int(r["id"]), version, digest)
            summary["deleted"] = len(delete_ids)
            summary["orphans"] = len(orphans)

    # --- one bulk upsert -------------------------------------------------------
    if pushes:
        try:
            result = client.upsert_events([p for _, _, _, p in pushes])
        except _API_ERRORS as exc:
            summary["errors"].append(f"upsert_events: {exc}")
            log.error("reconcile: bulk upsert of %d events failed: %s", len(pushes), exc)
        else:
            ids = {e.get("external_id"): e.get("id") for e in result if isinstance(e, dict)}
            for r, version, digest, _ in pushes:
                icu_id = ids.get(r["external_id"])
                if not sync.mark_pushed(conn, int(r["id"]), version, digest, int(icu_id) if icu_id else None):
                    log.info("reconcile: %s changed during the push; stays dirty", r["external_id"])
            summary["pushed"] = len(pushes)
    return summary


def _missing(conn: sqlite3.Connection, row: Row, summary: dict[str, Any]) -> None:
    """A clean, pushed row has no remote event. Two runs in a row: Lapo deleted it."""
    ext, sid = str(row["external_id"]), int(row["id"])
    misses = sync.record_missing_poll(conn, sid)
    summary["missing"] += 1
    if misses < MISSES_MEAN_DELETED:
        return
    if row["user_locked"]:
        log.warning("reconcile: locked %s is missing from intervals.icu (%d runs); leaving it", ext, misses)
        return
    res = plan_ops.skip_session(conn, ext, actor=ACTOR, reason="deleted in intervals.icu")
    if res.status != "ok" or res.row is None:
        log.warning("reconcile: could not skip %s after it vanished: %s", ext, res.message)
        return
    # The remote event is already gone: the skip needs no push.
    sync.mark_pushed(conn, sid, int(res.row["row_version"]), sync.content_hash(res.row))
    summary["treated_deleted"].append(ext)
    log.info("reconcile: %s missing %d runs in a row; treated as deleted by Lapo", ext, misses)
