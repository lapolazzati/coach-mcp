"""U10: coach-mcp write tools, called through an in-process FastMCP client."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from coach.db.migrate import migrate
from coach.mcp import tools_write
from coach.plan.reconcile import reconcile
from coach.store import thresholds
from coach.store.db import connect_write
from tests.store_helpers import TODAY, add_session, session
from tests.test_reconcile import FakeClient

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
DAY = TODAY.isoformat()


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


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


def server(path: Path, client: FakeClient | None = None) -> FastMCP:
    mcp = FastMCP("t")
    tools_write.register(mcp, path, client_factory=(lambda: client) if client else None,  # type: ignore[arg-type,return-value]
                         today=lambda: TODAY, now=lambda: NOW)
    return mcp


async def call(path: Path, tool: str, client: FakeClient | None = None, **args: Any) -> dict[str, Any]:
    async with Client(server(path, client)) as c:
        result = await c.call_tool(tool, args)
    assert result.structured_content is not None
    return result.structured_content


def fresh(path: Path) -> sqlite3.Connection:
    """A new connection, so reads see what the tool's own connection committed."""
    return connect_write(path)


@pytest.mark.anyio
async def test_log_meal_summary_has_today_total(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    await call(path, "log_meal", description="oats", kcal=550, local_date=DAY, confidence=0.8)
    out = await call(path, "log_meal", description="pasta", kcal=700, carbs_g=110, local_date=DAY,
                     confidence=0.6)
    assert out["status"] == "created"
    assert out["day_kcal_total"] == 1250
    assert out["summary"] == f"Logged pasta (700 kcal); {DAY} total 1250 kcal"
    assert out["audit_id"]
    bad = await call(path, "log_meal", description="x", kcal=-1)
    assert "kcal must be >= 0" in bad["error"]


@pytest.mark.anyio
async def test_repeated_client_ref_returns_existing(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    first = await call(path, "log_meal", description="oats", kcal=550, local_date=DAY, client_ref="m-1")
    again = await call(path, "log_meal", description="oats", kcal=550, local_date=DAY, client_ref="m-1")
    assert first["status"] == "created" and again["status"] == "existing"
    assert again["day_kcal_total"] == 550
    assert again["summary"].startswith("Already logged")
    note = await call(path, "add_note", text="legs heavy", tags=["fatigue"], local_date=DAY, client_ref="n-1")
    note2 = await call(path, "add_note", text="legs heavy", tags=["fatigue"], local_date=DAY, client_ref="n-1")
    assert note["status"] == "created" and note2["status"] == "existing"


@pytest.mark.anyio
async def test_implausible_weight_needs_confirmation(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    for i in range(1, 4):
        conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?, 80, 'garmin', 'x')",
                     ((TODAY - timedelta(days=i)).isoformat(),))
    conn.commit()
    out = await call(path, "log_weight", kg=88.0, local_date=DAY)
    assert out["status"] == "needs_confirmation"
    assert "implausible" in out["flags"]
    assert "ask Lapo" in out["summary"]
    check = fresh(path)
    assert check.execute("SELECT count(*) FROM weight WHERE date = ?", (DAY,)).fetchone()[0] == 0
    check.close()
    ok = await call(path, "log_weight", kg=88.0, local_date=DAY, confirm=True)
    assert ok["status"] == "created"


@pytest.mark.anyio
async def test_second_weight_same_day_says_replaced(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    first = await call(path, "log_weight", kg=80.2, local_date=DAY)
    second = await call(path, "log_weight", kg=79.9, local_date=DAY)
    assert first["status"] == "created"
    assert second["status"] == "replaced"
    assert "replaced 80.2 kg" in second["summary"]
    assert second["weight"]["kg"] == 79.9


@pytest.mark.anyio
async def test_move_session_deletes_old_event_and_upserts_new(db: tuple[Path, sqlite3.Connection],
                                                              fake: FakeClient) -> None:
    path, conn = db
    old = add_session(conn, "2026-11-10", "am")
    reconcile(conn, fake, now=NOW)
    assert old in fake.events
    fake.calls.clear()

    out = await call(path, "move_session", fake, external_id=old, new_date="2026-11-11")
    new = "coach:2026-11-11:am"
    assert out["status"] == "ok"
    assert out["sync"] == {"status": "synced"}
    assert ("delete", [old]) in fake.writes()
    assert ("upsert", [new]) in fake.writes()
    assert set(fake.events) == {new}
    assert out["summary"].endswith("intervals.icu synced")
    assert out["audit_id"]


@pytest.mark.anyio
async def test_lock_sets_user_locked_otherwise_only_pinned(db: tuple[Path, sqlite3.Connection],
                                                           fake: FakeClient) -> None:
    path, conn = db
    a = add_session(conn, "2026-11-10", "am")
    b = add_session(conn, "2026-11-12", "am")
    await call(path, "move_session", fake, external_id=a, new_date="2026-11-11")
    await call(path, "move_session", fake, external_id=b, new_date="2026-11-13", lock=True)
    check = fresh(path)
    plain = session(check, "coach:2026-11-11:am")
    locked = session(check, "coach:2026-11-13:am")
    check.close()
    assert plain is not None and locked is not None
    assert plain["pinned_date"] == 1 and plain["user_locked"] == 0
    assert locked["pinned_date"] == 1 and locked["user_locked"] == 1


@pytest.mark.anyio
async def test_intervals_down_keeps_change_and_reports_pending(db: tuple[Path, sqlite3.Connection],
                                                               fake: FakeClient) -> None:
    path, conn = db
    ext = add_session(conn, "2026-11-10", "am")
    fake.fail_upsert = True
    out = await call(path, "move_session", fake, external_id=ext, new_date="2026-11-11")
    assert out["status"] == "ok"
    assert out["sync"]["status"] == "pending"
    assert out["summary"].endswith("intervals.icu pending")
    check = fresh(path)
    row = session(check, "coach:2026-11-11:am")
    check.close()
    assert row is not None and row["sync_dirty"] == 1
    # No client configured at all is also pending, never an error.
    other = add_session(conn, "2026-11-12", "am")
    out = await call(path, "skip_session", None, external_id=other)
    assert out["status"] == "ok" and out["sync"]["status"] == "pending"


@pytest.mark.anyio
async def test_undo_change_reverts_and_resyncs(db: tuple[Path, sqlite3.Connection], fake: FakeClient) -> None:
    path, conn = db
    old = add_session(conn, "2026-11-10", "am")
    moved = await call(path, "move_session", fake, external_id=old, new_date="2026-11-11")
    assert set(fake.events) == {"coach:2026-11-11:am"}
    out = await call(path, "undo_change", fake, audit_id=moved["audit_id"])
    assert out["status"] == "undone"
    assert out["sync"] == {"status": "synced"}
    assert set(fake.events) == {old}
    check = fresh(path)
    row = session(check, old)
    check.close()
    assert row is not None and row["date"] == "2026-11-10" and row["sync_dirty"] == 0
    again = await call(path, "undo_change", fake, audit_id=moved["audit_id"])
    assert again["status"] == "refused"


@pytest.mark.anyio
async def test_save_month_plan_summary_includes_warnings(db: tuple[Path, sqlite3.Connection],
                                                         fake: FakeClient) -> None:
    path, _ = db
    sessions = [{"date": (TODAY + timedelta(days=i)).isoformat(), "slot": "am", "sport": "ride",
                 "kind": "endurance", "name": f"Z2 {i}", "duration_min": 90, "steps_text": "- 90m 65%"}
                for i in range(9)]
    out = await call(path, "save_month_plan", fake, month="2026-11", rationale="Base", sessions=sessions)
    assert out["saved"] == 9
    assert out["warnings"], out
    assert "warnings:" in out["summary"] and out["warnings"][0] in out["summary"]
    assert out["sync"] == {"status": "synced"}
    bad = await call(path, "save_month_plan", fake, month="2026-11", rationale="x",
                     sessions=[{**sessions[0], "steps_text": "- banana"}])
    assert bad["problems"] and "error" in bad


@pytest.mark.anyio
async def test_tool_listing_annotations_match_ktd6(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    tools = {t.name: t for t in await server(path).list_tools()}
    logging = {"log_meal", "log_weight", "add_note", "log_supplement", "set_supplement", "save_weekly_review",
               "set_athlete_settings"}
    plan = {"move_session", "skip_session", "swap_sessions", "save_month_plan", "undo_change"}
    assert set(tools) == logging | plan
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None and ann.readOnlyHint is False, name
        assert tool.description and "\n" not in tool.description.strip(), name
        assert "client_ref" in tool.parameters["properties"], name
        if name in logging:
            assert ann.idempotentHint is True and ann.destructiveHint is False, name
        else:
            assert ann.destructiveHint is True, name
    for name in ("log_meal", "log_weight"):
        assert "ask Lapo" in (tools[name].description or ""), name
    assert "lock=true only when Lapo says" in (tools["move_session"].description or "")
    assert (tools["save_month_plan"].description or "").count("- ") >= 3


@pytest.mark.anyio
async def test_set_athlete_settings_saves_and_undoes(db: tuple[Path, sqlite3.Connection], fake: FakeClient) -> None:
    path, _ = db
    out = await call(path, "set_athlete_settings", fake, ftp_w=185, weight_kg=72, effective_from="2026-11-01")
    assert out["status"] == "saved" and out["settings"]["ftp_w"]["value"] == 185
    upd = await call(path, "set_athlete_settings", fake, ftp_w=190, effective_from="2026-11-01", note="coach")
    assert upd["previous"]["ftp_w"]["value"] == 185
    assert (await call(path, "undo_change", fake, audit_id=upd["audit_ids"]["ftp_w"]))["status"] == "undone"
    check = fresh(path)
    assert thresholds.effective(check, "2026-11-06")["ftp_w"]["value"] == 185
    check.close()
    assert (await call(path, "undo_change", fake, audit_id=out["audit_ids"]["ftp_w"]))["status"] == "undone"
    check = fresh(path)
    eff = thresholds.effective(check, "2026-11-06")
    check.close()
    assert "ftp_w" not in eff and eff["weight_kg"]["value"] == 72


@pytest.mark.anyio
async def test_set_athlete_settings_defaults_and_validation(db: tuple[Path, sqlite3.Connection],
                                                           fake: FakeClient) -> None:
    path, _ = db
    assert "error" in await call(path, "set_athlete_settings", fake)
    assert "error" in await call(path, "set_athlete_settings", fake, ftp_w=600)
    bad = await call(path, "set_athlete_settings", fake, ftp_w=190, weight_kg=20)
    assert "error" in bad
    check = fresh(path)
    assert thresholds.effective(check, DAY) == {}
    check.close()
    ok = await call(path, "set_athlete_settings", fake, ftp_w=190)
    assert ok["settings"]["ftp_w"]["effective_from"] == DAY
