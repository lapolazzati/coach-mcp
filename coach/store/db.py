"""Write connections and transactions for the store layer.

Two processes (coach and coach-mcp) write the same SQLite file, so every write
connection runs in WAL mode with a busy timeout (KTD4). Timestamps are UTC
ISO-8601; dates are Europe/Rome local (KTD8 of the 2026-11-03 plan).
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
BUSY_TIMEOUT_MS = 5000
MIN_SCHEMA_VERSION = 4

Row = dict[str, Any]

_savepoints = itertools.count()


class SchemaTooOld(RuntimeError):
    """The DB has not been migrated to the version the store needs."""


def connect_write(db_path: Path | str) -> sqlite3.Connection:
    """Open a write connection: WAL, busy_timeout and foreign keys on."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    configure(conn)
    return conn


def configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


def require_schema(conn: sqlite3.Connection, min_version: int = MIN_SCHEMA_VERSION) -> int:
    """Return the schema version, or raise SchemaTooOld when it is below min_version."""
    try:
        version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] or 0
    except sqlite3.OperationalError:
        version = 0
    if version < min_version:
        raise SchemaTooOld(f"schema version {version} < {min_version}; run coach.db.migrate")
    return int(version)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One atomic unit of work. Nests through savepoints when a transaction is open."""
    if conn.in_transaction:
        name = f"store_sp_{next(_savepoints)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        conn.execute(f"RELEASE SAVEPOINT {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Row | None:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return {d[0]: v for d, v in zip(cur.description, row, strict=True)}


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[Row]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_local() -> date:
    return datetime.now(TZ).date()


def local_date_of(ts_utc: str) -> str:
    """Europe/Rome calendar date of a UTC ISO-8601 timestamp."""
    dt = datetime.fromisoformat(ts_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(TZ).date().isoformat()


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not a YYYY-MM-DD date: {value!r}") from exc
