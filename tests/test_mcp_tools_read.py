"""U9: coach-mcp read tools, called through an in-process FastMCP client."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from coach.db.migrate import migrate
from coach.ingest.intervals_sync import ACTIVITIES_KEY, WELLNESS_KEY
from coach.mcp import format as fmt
from coach.mcp import tools_read
from coach.store.db import connect_write
from tests.store_helpers import TODAY, add_session

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
TS = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    path = tmp_path / "coach.db"
    conn = connect_write(path)
    migrate(conn)
    yield path, conn
    conn.close()


def server(path: Path) -> FastMCP:
    mcp = FastMCP("t")
    tools_read.register(mcp, path, today=lambda: TODAY, now=lambda: NOW)
    return mcp


async def call(path: Path, tool: str, **args: Any) -> dict[str, Any]:
    async with Client(server(path)) as client:
        result = await client.call_tool(tool, args)
    assert result.structured_content is not None
    return result.structured_content


def synced(conn: sqlite3.Connection, at: datetime) -> None:
    for key in (WELLNESS_KEY, ACTIVITIES_KEY):
        conn.execute("INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)",
                     (key, at.strftime(TS), at.strftime(TS)))
    conn.commit()


def seed_today(conn: sqlite3.Connection) -> None:
    day = TODAY.isoformat()
    conn.execute("INSERT INTO readiness_day (date, status, flags_json, signals_json, computed_at)"
                 " VALUES (?, 'amber', ?, ?, ?)",
                 (day, json.dumps(["hrv_low", "sleep_short"]), json.dumps({"hrv": {"value": 40}}), NOW.strftime(TS)))
    for i, kcal in enumerate((550, 700)):
        conn.execute("INSERT INTO meal (ts_utc, local_date, description, kcal, source) VALUES (?, ?, ?, ?, 'photo')",
                     (f"{day}T0{7 + i * 4}:00:00Z", day, f"meal {i}", kcal))
    for d, kg in ((TODAY - timedelta(days=2), 80.4), (TODAY - timedelta(days=1), 80.0), (TODAY, 79.8)):
        conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?, ?, 'garmin', ?)",
                     (d.isoformat(), kg, NOW.strftime(TS)))
    conn.execute("INSERT INTO supplement (name, dose, timing, active) VALUES ('creatine', '5 g', 'morning', 1)")
    conn.commit()
    add_session(conn, day, "pm", name="2x20 sweet spot", ease_reason="HRV low")
    synced(conn, NOW - timedelta(hours=2))


def seed_history(conn: sqlite3.Connection, days: int) -> None:
    for i in range(days):
        d = (TODAY - timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO wellness_day (date, ctl, atl, hrv_ms, resting_hr, sleep_secs, updated_at)"
                     " VALUES (?, ?, ?, 60, 48, 27000, ?)", (d, 50 - i * 0.05, 55 - i * 0.04, NOW.strftime(TS)))
        if i % 3 == 0:
            cur = conn.execute(
                "INSERT INTO activity (source, sport, sport_group, name, start_utc, start_local, duration_s,"
                " training_load, updated_at) VALUES ('intervals', 'Ride', 'cycling', 'ride', ?, ?, 3600, 70, ?)",
                (f"{d}T08:00:00Z", f"{d}T09:00:00", NOW.strftime(TS)))
            for secs, watts in ((5, 900), (60, 450), (300, 330), (1200, 290 - i % 7), (3600, 250)):
                conn.execute("INSERT INTO activity_power_best VALUES (?, ?, ?)", (cur.lastrowid, secs, watts))
    conn.commit()
    synced(conn, NOW - timedelta(hours=1))


@pytest.mark.anyio
async def test_get_today_returns_readiness_session_and_meals(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    seed_today(conn)
    out = await call(path, "get_today")
    assert out["readiness"]["status"] == "amber"
    assert out["readiness"]["reasons"] == ["hrv_low", "sleep_short"]
    assert out["session"]["name"] == "2x20 sweet spot"
    assert out["session"]["ease_reason"] == "HRV low"
    assert out["meals"]["kcal_total"] == 1250
    assert len(out["meals"]["items"]) == 2
    assert out["weight"]["latest"]["kg"] == 79.8
    assert out["weight"]["mean_7d"] == pytest.approx(80.07, abs=0.01)
    assert out["supplements"][0]["name"] == "creatine" and out["supplements"][0]["taken_today"] is False
    assert out["as_of"] == NOW.strftime(TS)
    assert out["freshness"]["stale"] is False


@pytest.mark.anyio
async def test_get_today_without_session_is_a_rest_day(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    synced(conn, NOW)
    out = await call(path, "get_today")
    assert out["session"] is None
    assert out["label"] == "rest day"
    assert out["readiness"]["status"] == "unknown"
    assert out["meals"]["kcal_total"] == 0


@pytest.mark.anyio
async def test_get_trends_twelve_months_is_bucketed_and_capped(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    seed_history(conn, 400)
    out = await call(path, "get_trends", metrics=["ctl", "weight", "hours", "load"], months=12)
    assert 50 <= len(out["buckets"]) <= 53
    assert len(json.dumps(out)) < fmt.MAX_CHARS
    assert out["truncated"] is False
    assert out["slope_per_bucket"]["ctl"] > 0  # ctl rises towards today
    monthly = await call(path, "get_trends", metrics=["ctl"], months=12, bucket="month")
    assert len(monthly["buckets"]) == 12


@pytest.mark.anyio
async def test_get_trends_fitness_and_power_bests(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    seed_history(conn, 120)
    out = await call(path, "get_trends", metrics=["ctl", "atl", "form", "power_bests"], months=3)
    last = out["buckets"][-1]
    assert last["ctl"] is not None and last["atl"] is not None
    assert last["form"] == pytest.approx(last["ctl"] - last["atl"], abs=0.02)
    bests = out["power_bests"]
    assert bests["current"]["20m"] == 290 and bests["current"]["5s"] == 900
    assert set(bests["window"]) == {"5s", "1m", "5m", "20m", "60m"}


@pytest.mark.anyio
async def test_end_before_start_is_a_clear_error(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    for tool in ("get_wellness", "get_training", "get_meals", "get_notes"):
        out = await call(path, tool, start="2026-11-06", end="2026-11-01")
        assert "end (2026-11-01) is before start (2026-11-06)" in out["error"]
    bad = await call(path, "get_wellness", start="yesterday")
    assert "ISO date" in bad["error"]


@pytest.mark.anyio
async def test_no_ingest_in_24h_is_stale(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    out = await call(path, "get_wellness")
    assert out["freshness"]["stale"] is True  # never synced
    synced(conn, NOW - timedelta(hours=25))
    out = await call(path, "get_wellness")
    assert out["freshness"]["stale"] is True
    assert out["freshness"]["wellness"]["stale"] is True
    synced(conn, NOW - timedelta(hours=3))
    out = await call(path, "get_wellness")
    assert out["freshness"]["stale"] is False


@pytest.mark.anyio
async def test_tool_listing_exposes_read_only_annotations(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    tools = await server(path).list_tools()
    names = {t.name for t in tools}
    assert names == {"get_today", "get_wellness", "get_training", "get_trends", "get_plan", "get_notes",
                     "get_meals", "get_supplements", "get_week_summary"}
    for tool in tools:
        assert tool.description and "\n" not in tool.description.strip(), tool.name
        assert tool.annotations is not None and tool.annotations.readOnlyHint is True, tool.name


def test_cap_drops_oldest_items_and_flags_truncation() -> None:
    big = {"days": [{"date": str(i), "pad": "x" * 100} for i in range(1000)]}
    out = fmt.cap(big)
    assert out["truncated"] is True
    assert len(json.dumps(out)) <= fmt.MAX_CHARS
    assert out["days"][-1]["date"] == "999"


def test_week_summary_and_plan_read_back(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    monday = TODAY - timedelta(days=TODAY.weekday())
    conn.execute("INSERT INTO weekly_summary VALUES (?, ?, ?)", (monday.isoformat(), '{"hours": 7.5}', "x"))
    conn.execute("INSERT INTO weekly_review (week_start, text, saved_at) VALUES (?, 'Solid week.', 'x')",
                 (monday.isoformat(),))
    conn.execute("INSERT INTO month_plan (month, rationale, created_at, updated_at) VALUES ('2026-11', 'Base', 'x', 'x')")
    conn.commit()
    add_session(conn, "2026-11-10")
    ro = fmt.connect_ro(path)
    try:
        assert date.fromisoformat(ro.execute("SELECT max(date) FROM planned_session").fetchone()[0]) > TODAY
    finally:
        ro.close()

    import anyio

    async def both() -> tuple[dict[str, Any], dict[str, Any]]:
        return await call(path, "get_week_summary"), await call(path, "get_plan")

    week, plan = anyio.run(both)
    assert week["summary"] == {"hours": 7.5} and week["review"]["text"] == "Solid week."
    assert plan["rationale"] == "Base" and plan["sessions"][0]["external_id"] == "coach:2026-11-10:am"
