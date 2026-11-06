"""Audit rows and undo for the user-visible tables (KTD8).

Representation (uses the 003 `audit` table as-is):
- actor: "claude" (Lapo via MCP) or "job" / "job:<name>".
- entity: the table name; entity_id: the row's primary key as text
  (comma-joined when one op touches several rows, e.g. a swap).
- before_json / after_json: the full row before and after (JSON object), null
  for "did not exist", or a JSON list of rows for multi-row ops.
- undone: 1 once undo() has replayed before_json. undo() itself writes a new
  audit row (action "undo:<action>"), so an undo is auditable too.

undo() refuses when the current row no longer equals after_json. For
planned_session, sync bookkeeping (sync_dirty, hashes, remote ids, row_version,
missing_polls, updated_at, matched activity) is ignored in that comparison and
never restored: a push between the op and the undo must not block the undo.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from coach.store import sync
from coach.store.db import Row, fetch_one, now_utc, transaction

ENTITY_PK: dict[str, str] = {
    "planned_session": "id",
    "meal": "id",
    "weight": "id",
    "note": "id",
    "supplement": "id",
    "supplement_intake": "id",
    "weekly_review": "week_start",
}

SESSION_BOOKKEEPING = frozenset({
    "icu_event_id", "last_written_hash", "gcal_event_id", "sync_dirty", "row_version", "missing_polls",
    "updated_at", "matched_activity_id",
})

# Audit rows that record a decision without a change: nothing to undo.
NOT_UNDOABLE = frozenset({"ease_advice"})

Snapshot = Row | list[Row] | None


def check_actor(actor: str) -> str:
    if actor == "claude" or is_job(actor):
        return actor
    raise ValueError(f"unknown actor {actor!r}; expected 'claude' or 'job:<name>'")


def is_job(actor: str) -> bool:
    return actor == "job" or (actor.startswith("job:") and len(actor) > 4)


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def record(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    entity: str,
    entity_id: str | int | None,
    before: Any,
    after: Any,
    reason: str | None = None,
) -> int:
    check_actor(actor)
    cur = conn.execute(
        "INSERT INTO audit (ts_utc, actor, action, entity, entity_id, before_json, after_json, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now_utc(), actor, action, entity, None if entity_id is None else str(entity_id), _dump(before),
         _dump(after), reason),
    )
    return int(cur.lastrowid or 0)


def get(conn: sqlite3.Connection, audit_id: int) -> Row | None:
    return fetch_one(conn, "SELECT * FROM audit WHERE id = ?", (audit_id,))


def snapshot(conn: sqlite3.Connection, entity: str, pk: Any) -> Row | None:
    pk_col = ENTITY_PK[entity]
    return fetch_one(conn, f"SELECT * FROM {entity} WHERE {pk_col} = ?", (pk,))


@dataclass
class UndoResult:
    status: str  # undone | refused | not_found
    message: str
    audit_id: int | None = None  # the new "undo:..." audit row
    rows: list[Row] = field(default_factory=list)


def _pairs(before_json: str | None, after_json: str | None) -> list[tuple[Row | None, Row | None]]:
    before = json.loads(before_json) if before_json else None
    after = json.loads(after_json) if after_json else None
    if isinstance(before, list) or isinstance(after, list):
        b = before if isinstance(before, list) else []
        a = after if isinstance(after, list) else []
        if len(b) != len(a):
            raise ValueError("multi-row audit with mismatched before/after")
        return list(zip(b, a, strict=True))
    return [(before, after)]


def _roundtrip(row: Row | None) -> Row | None:
    return None if row is None else json.loads(json.dumps(row, default=str))


def _differences(entity: str, current: Row | None, expected: Row | None) -> list[str]:
    cur, exp = _roundtrip(current), _roundtrip(expected)
    if cur is None or exp is None:
        return [] if cur is exp else ["row exists" if cur is not None else "row is gone"]
    ignore = SESSION_BOOKKEEPING if entity == "planned_session" else frozenset()
    keys = (set(cur) | set(exp)) - ignore
    return sorted(k for k in keys if cur.get(k) != exp.get(k))


def undo(conn: sqlite3.Connection, audit_id: int, *, actor: str = "claude") -> UndoResult:
    """Replay before_json for audit_id and mark it undone; refuse if the row changed since."""
    check_actor(actor)
    with transaction(conn):
        entry = get(conn, audit_id)
        if entry is None:
            return UndoResult("not_found", f"no audit entry {audit_id}")
        if entry["undone"]:
            return UndoResult("refused", f"audit entry {audit_id} was already undone")
        if entry["action"] in NOT_UNDOABLE:
            return UndoResult("refused", f"audit entry {audit_id} ({entry['action']}) changed nothing")
        entity = entry["entity"]
        if entity not in ENTITY_PK:
            return UndoResult("refused", f"undo is not supported for {entity!r}")
        pk_col = ENTITY_PK[entity]
        pairs = _pairs(entry["before_json"], entry["after_json"])

        currents: list[Row | None] = []
        for before, after in pairs:
            ref = after if after is not None else before
            if ref is None:
                return UndoResult("refused", f"audit entry {audit_id} has no row snapshot")
            current = snapshot(conn, entity, ref[pk_col])
            diff = _differences(entity, current, after)
            if diff:
                return UndoResult(
                    "refused",
                    f"{entity} {ref[pk_col]} changed since audit {audit_id} ({', '.join(diff)}); not undoing",
                )
            currents.append(current)

        if entity == "planned_session":
            refusal = _check_session_slots(conn, pairs)
            if refusal:
                return UndoResult("refused", refusal)
            restored = _restore_sessions(conn, pairs, currents, audit_id)
        else:
            restored = [_restore_row(conn, entity, pk_col, cur, before)
                        for (before, _), cur in zip(pairs, currents, strict=True)]

        multi = len(pairs) > 1
        new_id = record(
            conn,
            actor=actor,
            action=f"undo:{entry['action']}",
            entity=entity,
            entity_id=entry["entity_id"],
            before=currents if multi else currents[0],
            after=restored if multi else restored[0],
            reason=f"undo of audit {audit_id}",
        )
        conn.execute("UPDATE audit SET undone = 1 WHERE id = ?", (audit_id,))
    rows = [r for r in restored if r is not None]
    return UndoResult("undone", f"undid audit {audit_id} ({entry['action']})", new_id, rows)


def _restore_row(conn: sqlite3.Connection, entity: str, pk_col: str, current: Row | None,
                 before: Row | None) -> Row | None:
    pk = (current or before or {}).get(pk_col)
    if before is None:
        conn.execute(f"DELETE FROM {entity} WHERE {pk_col} = ?", (pk,))
        return None
    if current is None:
        cols = ", ".join(before)
        marks = ", ".join("?" for _ in before)
        conn.execute(f"INSERT INTO {entity} ({cols}) VALUES ({marks})", tuple(before.values()))
    else:
        values = {k: v for k, v in before.items() if k != pk_col}
        sets, params = sync.as_params(values)
        conn.execute(f"UPDATE {entity} SET {sets} WHERE {pk_col} = ?", (*params, pk))
    return snapshot(conn, entity, pk)


def _check_session_slots(conn: sqlite3.Connection, pairs: list[tuple[Row | None, Row | None]]) -> str | None:
    """Every external_id the undo restores must be free or held by a row in this undo."""
    ids = {int((a or b or {})["id"]) for b, a in pairs}
    for before, _ in pairs:
        if before is None:
            continue
        holder = conn.execute("SELECT id FROM planned_session WHERE external_id = ?",
                              (before["external_id"],)).fetchone()
        if holder is not None and holder[0] not in ids:
            return (f"{before['external_id']} is now taken by another session (id {holder[0]}); "
                    "move that one first")
    return None


def _restore_sessions(conn: sqlite3.Connection, pairs: list[tuple[Row | None, Row | None]],
                      currents: list[Row | None], audit_id: int) -> list[Row | None]:
    changes: list[tuple[int, str, str]] = []
    for (before, _), current in zip(pairs, currents, strict=True):
        if before is not None and current is not None:
            changes.append((int(current["id"]), current["external_id"], before["external_id"]))
    sync.reassign_external_ids(conn, changes, reason=f"undo:{audit_id}")

    out: list[Row | None] = []
    for (before, _), current in zip(pairs, currents, strict=True):
        if before is None:  # undo of a create: the event must go too
            assert current is not None
            sync.add_tombstone(conn, current["external_id"], int(current["id"]), f"undo:{audit_id}")
            conn.execute("DELETE FROM planned_session WHERE id = ?", (current["id"],))
            out.append(None)
            continue
        values = {k: v for k, v in before.items() if k not in SESSION_BOOKKEEPING and k not in ("id", "external_id")}
        if current is None:
            restored = dict(before)
            restored.update(sync_dirty=1, last_written_hash=None, icu_event_id=None, updated_at=now_utc())
            sync.claim_external_id(conn, before["external_id"])
            cols = ", ".join(restored)
            marks = ", ".join("?" for _ in restored)
            conn.execute(f"INSERT INTO planned_session ({cols}) VALUES ({marks})", tuple(restored.values()))
        else:
            sets, params = sync.as_params(values)
            conn.execute(f"UPDATE planned_session SET {sets} WHERE id = ?", (*params, before["id"]))
        sync.mark_dirty(conn, int(before["id"]))
        out.append(snapshot(conn, "planned_session", before["id"]))
    return out
