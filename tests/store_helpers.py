"""Helpers shared by tests/test_store_*.py (plain module, not a conftest)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date

from coach.db.migrate import migrate
from coach.store.db import connect_write

TODAY = date(2026, 11, 6)  # a Friday; Tue 2026-11-10, Wed 2026-11-11


def fresh_db() -> Iterator[sqlite3.Connection]:
    c = connect_write(":memory:")
    migrate(c)
    yield c
    c.close()


def add_session(conn: sqlite3.Connection, day: str, slot: str = "am", **over: object) -> str:
    ext = f"coach:{day}:{slot}"
    values: dict[str, object] = {
        "external_id": ext, "date": day, "slot": slot, "duration_min": 90, "sport": "ride", "kind": "threshold",
        "name": "3x12 threshold", "session_json": json.dumps({"steps": ["3x12m 95-100%"]}),
        "updated_at": "2026-11-01T00:00:00Z",
    }
    values.update(over)
    cols = ", ".join(values)
    conn.execute(f"INSERT INTO planned_session ({cols}) VALUES ({', '.join('?' for _ in values)})",
                 tuple(values.values()))
    conn.commit()
    return ext


def session(conn: sqlite3.Connection, ext: str) -> dict[str, object] | None:
    cur = conn.execute("SELECT * FROM planned_session WHERE external_id = ?", (ext,))
    row = cur.fetchone()
    return None if row is None else {d[0]: v for d, v in zip(cur.description, row, strict=True)}


def count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    return int(conn.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0])
