"""Shared shape for coach-mcp tool results.

Every read result carries ``as_of`` (UTC) and ``freshness`` (when intervals.icu
last synced, and whether that is stale), and stays under ``MAX_CHARS`` once
serialised: the oldest list items are dropped first and ``truncated`` is set.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from coach.ingest.intervals_sync import ACTIVITIES_KEY, WELLNESS_KEY

MAX_CHARS = 50_000
STALE_AFTER = timedelta(hours=24)
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FRESHNESS_KEYS = {"wellness": WELLNESS_KEY, "activities": ACTIVITIES_KEY}

Result = dict[str, Any]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def connect_ro(db_path: Path | str) -> sqlite3.Connection:
    """Read-only connection: a read tool can never write, whatever SQL it runs."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def error(message: str) -> Result:
    return {"error": message}


def parse_day(value: str | None, default: date, name: str) -> date | str:
    """ISO date or the default; a string return is the error message."""
    if value in (None, ""):
        return default
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}"


def date_range(start: str | None, end: str | None, default_start: date, default_end: date) -> tuple[date, date] | str:
    """Resolve an inclusive date range; a string return is the error message."""
    s = parse_day(start, default_start, "start")
    if isinstance(s, str):
        return s
    e = parse_day(end, default_end, "end")
    if isinstance(e, str):
        return e
    if e < s:
        return f"end ({e}) is before start ({s}); swap them or pick a later end"
    return s, e


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def freshness(conn: sqlite3.Connection, now: datetime) -> Result:
    """Last good intervals.icu sync per feed; stale when any feed is missing or older than 24 h."""
    out: Result = {}
    stale = False
    for name, key in FRESHNESS_KEYS.items():
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        last = _parse_ts(row[0] if row else None)
        feed_stale = last is None or now - last > STALE_AFTER
        stale = stale or feed_stale
        out[name] = {"last_ok": row[0] if row else None, "stale": feed_stale}
    out["stale"] = stale
    if stale:
        out["note"] = "intervals.icu data may be out of date; mention it if it matters"
    return out


def _longest_list(value: Any) -> list[Any] | None:
    """The list (at any depth) with the largest serialised size."""
    best: list[Any] | None = None
    best_len = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            size = len(json.dumps(item, default=str))
            if len(item) > 1 and size > best_len:
                best, best_len = item, size
            stack.extend(item)
    return best


def cap(result: Result, max_chars: int = MAX_CHARS) -> Result:
    """Drop the oldest items of the biggest list until the result fits; flag it."""
    result.setdefault("truncated", False)
    while len(json.dumps(result, default=str)) > max_chars:
        biggest = _longest_list(result)
        if biggest is None:
            break
        del biggest[: max(1, len(biggest) // 4)]
        result["truncated"] = True
    return result


def envelope(conn: sqlite3.Connection, data: Result, now: datetime) -> Result:
    """Wrap a read result with as_of, freshness and the size cap."""
    out: Result = {"as_of": now.strftime(_TS_FORMAT), "freshness": freshness(conn, now)}
    out.update(data)
    return cap(out)


def written(summary: str, **payload: Any) -> Result:
    """A write result: one line for Lapo, then the details."""
    return {"summary": summary, **payload}
