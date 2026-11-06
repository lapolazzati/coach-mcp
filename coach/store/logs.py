"""Logging writes: meals, weight, notes, supplements, weekly review.

Each write runs in one transaction with its audit row and is idempotent:
a repeated client_ref returns the stored row with status "existing" and
writes nothing. Results carry the stored row so the MCP layer can echo it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from coach.store import audit
from coach.store.db import (
    Row,
    fetch_one,
    local_date_of,
    now_utc,
    parse_date,
    today_local,
    transaction,
)

WEIGHT_MIN_KG = 50.0
WEIGHT_MAX_KG = 150.0
WEIGHT_MAX_DEVIATION_KG = 2.5
WEIGHT_SOURCE = "claude"
MEAL_SOURCES = ("photo", "text")
SUPPLEMENT_TIMINGS = ("morning", "lunch", "evening", "with_meal")


@dataclass
class LogResult:
    status: str  # created | existing | replaced | updated | needs_confirmation | not_found
    row: Row | None = None
    audit_id: int | None = None
    previous: Row | None = None
    message: str = ""
    flags: list[str] = field(default_factory=list)


def _by_client_ref(conn: sqlite3.Connection, table: str, client_ref: str | None) -> Row | None:
    if not client_ref:
        return None
    return fetch_one(conn, f"SELECT * FROM {table} WHERE client_ref = ?", (client_ref,))


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> Row:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    row = fetch_one(conn, f"SELECT * FROM {table} WHERE rowid = ?", (cur.lastrowid,))
    assert row is not None
    return row


def _day(value: str | date | None, ts_utc: str | None = None) -> str:
    if value is not None:
        return parse_date(value).isoformat()
    if ts_utc is not None:
        return local_date_of(ts_utc)
    return today_local().isoformat()


# --- meals -----------------------------------------------------------------


def log_meal(
    conn: sqlite3.Connection,
    *,
    description: str,
    kcal: int | None,
    carbs_g: float | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    source: str = "photo",
    confidence: float | None = None,
    confirmed: bool = False,
    ts_utc: str | None = None,
    local_date: str | date | None = None,
    client_ref: str | None = None,
    actor: str = "claude",
    reason: str | None = None,
) -> LogResult:
    if source not in MEAL_SOURCES:
        raise ValueError(f"meal source must be one of {MEAL_SOURCES}")
    for name, value in (("kcal", kcal), ("carbs_g", carbs_g), ("protein_g", protein_g), ("fat_g", fat_g)):
        if value is not None and value < 0:
            raise ValueError(f"{name} must be >= 0")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    ts = ts_utc or now_utc()
    with transaction(conn):
        existing = _by_client_ref(conn, "meal", client_ref)
        if existing:
            return LogResult("existing", existing, message="already logged")
        row = _insert(conn, "meal", {
            "ts_utc": ts, "local_date": _day(local_date, ts), "description": description, "kcal": kcal,
            "carbs_g": carbs_g, "protein_g": protein_g, "fat_g": fat_g, "source": source,
            "confidence": confidence, "confirmed": int(confirmed), "client_ref": client_ref,
        })
        aid = audit.record(conn, actor=actor, action="log_meal", entity="meal", entity_id=row["id"],
                           before=None, after=row, reason=reason)
    return LogResult("created", row, aid)


# --- weight ----------------------------------------------------------------


def weight_mean_7d(conn: sqlite3.Connection, day: str) -> float | None:
    """Mean of the per-day mean weights over the 7 days before `day` (any source)."""
    start = (parse_date(day) - timedelta(days=7)).isoformat()
    row = conn.execute(
        "SELECT avg(kg) FROM (SELECT avg(kg) AS kg FROM weight WHERE date >= ? AND date < ? GROUP BY date)",
        (start, day),
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def weight_flags(conn: sqlite3.Connection, kg: float, day: str) -> list[str]:
    flags = []
    if not WEIGHT_MIN_KG <= kg <= WEIGHT_MAX_KG:
        flags.append(f"outside {WEIGHT_MIN_KG:g}-{WEIGHT_MAX_KG:g} kg")
    mean = weight_mean_7d(conn, day)
    if mean is not None and abs(kg - mean) > WEIGHT_MAX_DEVIATION_KG:
        flags.append(f"{kg - mean:+.1f} kg from 7-day mean {mean:.1f}")
    return flags


def log_weight(
    conn: sqlite3.Connection,
    *,
    kg: float,
    local_date: str | date | None = None,
    confirm: bool = False,
    client_ref: str | None = None,
    actor: str = "claude",
    reason: str | None = None,
) -> LogResult:
    """Upsert the Claude reading for the day. Implausible readings need confirm=True."""
    if kg <= 0:
        raise ValueError("kg must be > 0")
    day = _day(local_date)
    kg = round(float(kg), 2)
    with transaction(conn):
        existing = _by_client_ref(conn, "weight", client_ref)
        if existing:
            return LogResult("existing", existing, message="already logged")
        flags = weight_flags(conn, kg, day)
        if flags and not confirm:
            return LogResult("needs_confirmation", None, message="implausible reading; ask Lapo, then confirm",
                             flags=["implausible", *flags])
        previous = fetch_one(conn, "SELECT * FROM weight WHERE date = ? AND source = ?", (day, WEIGHT_SOURCE))
        if previous:
            conn.execute("UPDATE weight SET kg = ?, client_ref = ?, updated_at = ? WHERE id = ?",
                         (kg, client_ref, now_utc(), previous["id"]))
            row = fetch_one(conn, "SELECT * FROM weight WHERE id = ?", (previous["id"],))
            status = "replaced"
        else:
            row = _insert(conn, "weight", {"date": day, "kg": kg, "source": WEIGHT_SOURCE, "updated_at": now_utc(),
                                           "client_ref": client_ref})
            status = "created"
        assert row is not None
        aid = audit.record(conn, actor=actor, action="log_weight", entity="weight", entity_id=row["id"],
                           before=previous, after=row, reason=reason)
    out_flags = ["implausible", *flags] if flags else []
    return LogResult(status, row, aid, previous=previous, flags=out_flags,
                     message=f"replaced {previous['kg']} kg" if previous else "")


# --- notes -----------------------------------------------------------------


def add_note(
    conn: sqlite3.Connection,
    *,
    text: str,
    tags: list[str] | None = None,
    local_date: str | date | None = None,
    client_ref: str | None = None,
    actor: str = "claude",
) -> LogResult:
    if not text.strip():
        raise ValueError("note text is empty")
    clean_tags = [t.strip() for t in tags or [] if t.strip()]
    if any("," in t for t in clean_tags):
        raise ValueError("tags cannot contain commas")
    ts = now_utc()
    with transaction(conn):
        existing = _by_client_ref(conn, "note", client_ref)
        if existing:
            return LogResult("existing", existing, message="already logged")
        row = _insert(conn, "note", {
            "ts_utc": ts, "local_date": _day(local_date, ts), "text": text.strip(),
            "tags": ",".join(clean_tags) or None, "source": "claude" if actor == "claude" else actor,
            "client_ref": client_ref,
        })
        aid = audit.record(conn, actor=actor, action="add_note", entity="note", entity_id=row["id"],
                           before=None, after=row)
    return LogResult("created", row, aid)


# --- supplements -----------------------------------------------------------


def find_supplement(conn: sqlite3.Connection, ref: int | str) -> Row | None:
    if isinstance(ref, int):
        return fetch_one(conn, "SELECT * FROM supplement WHERE id = ?", (ref,))
    return fetch_one(conn, "SELECT * FROM supplement WHERE lower(name) = lower(?) ORDER BY active DESC, id",
                     (ref.strip(),))


def upsert_supplement(
    conn: sqlite3.Connection,
    *,
    name: str,
    timing: str,
    dose: str | None = None,
    active: bool = True,
    notes: str | None = None,
    supplement_id: int | None = None,
    actor: str = "claude",
) -> LogResult:
    """Create or update a supplement, matched by id or case-insensitive name."""
    if timing not in SUPPLEMENT_TIMINGS:
        raise ValueError(f"timing must be one of {SUPPLEMENT_TIMINGS}")
    if not name.strip():
        raise ValueError("supplement name is empty")
    values: dict[str, Any] = {"name": name.strip(), "dose": dose, "timing": timing, "active": int(active),
                              "notes": notes}
    with transaction(conn):
        current = find_supplement(conn, supplement_id if supplement_id is not None else name)
        if supplement_id is not None and current is None:
            return LogResult("not_found", message=f"no supplement {supplement_id}")
        if current is None:
            row = _insert(conn, "supplement", values)
            aid = audit.record(conn, actor=actor, action="upsert_supplement", entity="supplement",
                               entity_id=row["id"], before=None, after=row)
            return LogResult("created", row, aid)
        if all(current[k] == v for k, v in values.items()):
            return LogResult("existing", current, message="unchanged")
        conn.execute("UPDATE supplement SET name = ?, dose = ?, timing = ?, active = ?, notes = ? WHERE id = ?",
                     (*values.values(), current["id"]))
        updated = fetch_one(conn, "SELECT * FROM supplement WHERE id = ?", (current["id"],))
        aid = audit.record(conn, actor=actor, action="upsert_supplement", entity="supplement",
                           entity_id=current["id"], before=current, after=updated)
    return LogResult("updated", updated, aid, previous=current)


def log_supplement(
    conn: sqlite3.Connection,
    *,
    supplement: int | str,
    local_date: str | date | None = None,
    timing: str | None = None,
    client_ref: str | None = None,
    actor: str = "claude",
) -> LogResult:
    """Record an intake. The same supplement, day and timing twice is one intake."""
    if timing is not None and timing not in SUPPLEMENT_TIMINGS:
        raise ValueError(f"timing must be one of {SUPPLEMENT_TIMINGS}")
    ts = now_utc()
    day = _day(local_date, ts)
    with transaction(conn):
        existing = _by_client_ref(conn, "supplement_intake", client_ref)
        if existing:
            return LogResult("existing", existing, message="already logged")
        sup = find_supplement(conn, supplement)
        if sup is None:
            return LogResult("not_found", message=f"no supplement {supplement!r}; add it with upsert_supplement")
        timing = timing or sup["timing"]
        dup = fetch_one(conn, "SELECT * FROM supplement_intake WHERE supplement_id = ? AND local_date = ?"
                              " AND timing IS ?", (sup["id"], day, timing))
        if dup:
            return LogResult("existing", dup, message="already logged for that day and timing")
        row = _insert(conn, "supplement_intake", {"supplement_id": sup["id"], "local_date": day, "timing": timing,
                                                  "ts_utc": ts, "client_ref": client_ref})
        aid = audit.record(conn, actor=actor, action="log_supplement", entity="supplement_intake",
                           entity_id=row["id"], before=None, after=row)
    return LogResult("created", row, aid)


# --- weekly review ---------------------------------------------------------


def save_weekly_review(
    conn: sqlite3.Connection,
    *,
    week_start: str | date,
    text: str,
    actor: str = "claude",
) -> LogResult:
    """Store the prose review for a week (Monday). Same text again is a no-op; new text replaces."""
    monday = parse_date(week_start)
    if monday.weekday() != 0:
        raise ValueError(f"week_start must be a Monday, got {monday.isoformat()}")
    if not text.strip():
        raise ValueError("review text is empty")
    key = monday.isoformat()
    with transaction(conn):
        previous = fetch_one(conn, "SELECT * FROM weekly_review WHERE week_start = ?", (key,))
        if previous and previous["text"] == text.strip():
            return LogResult("existing", previous, message="unchanged")
        source = "claude" if actor == "claude" else actor
        conn.execute(
            "INSERT INTO weekly_review (week_start, text, source, saved_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (week_start) DO UPDATE SET text = excluded.text, source = excluded.source,"
            " saved_at = excluded.saved_at",
            (key, text.strip(), source, now_utc()),
        )
        row = fetch_one(conn, "SELECT * FROM weekly_review WHERE week_start = ?", (key,))
        aid = audit.record(conn, actor=actor, action="save_weekly_review", entity="weekly_review", entity_id=key,
                           before=previous, after=row)
    return LogResult("replaced" if previous else "created", row, aid, previous=previous)
