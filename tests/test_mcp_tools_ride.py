"""U6: coach-mcp ride tools (sync_now, settings, activity, segment, power curve, streams)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from coach.db.migrate import migrate
from coach.ingest import garmin_sync as gs
from coach.integrations.garmin import GarminError
from coach.mcp import tools_ride
from coach.store import thresholds
from coach.store.db import connect_write
from tests.test_garmin_sync_activities import D0, D1, GID, FakeGarmin, summary

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TODAY = D1
MAX_DEFAULT_CHARS = 6000


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
def ride(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> tuple[Path, int]:
    path, conn = db
    gs.sync_activities(FakeGarmin([summary()]), conn, tmp_path / "fit", D0, D1)
    (act_id,) = conn.execute("SELECT id FROM activity WHERE garmin_activity_id = ?", (GID,)).fetchone()
    return path, act_id


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def server(path: Path, clock: Clock | None = None, **kw: Any) -> FastMCP:
    mcp = FastMCP("t")
    tools_ride.register(mcp, path, today=lambda: TODAY, now=clock or Clock(), **kw)
    return mcp


async def call_on(mcp: FastMCP, tool: str, **args: Any) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
    assert result.structured_content is not None
    return result.structured_content


async def call(path: Path, tool: str, **args: Any) -> dict[str, Any]:
    return await call_on(server(path), tool, **args)


def size(data: dict[str, Any]) -> int:
    return len(json.dumps(data, separators=(",", ":")))


# get_activity


@pytest.mark.anyio
async def test_get_activity_detail(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    out = await call(path, "get_activity", id=act_id)
    assert out["activity"]["garmin_activity_id"] == GID
    assert out["garmin"]["tss"] == 25.0 and out["garmin"]["intensity_factor"] == 0.8
    assert out["computed"]["np"] > 0 and out["computed"]["work_kj"] > 0
    assert [lap["idx"] for lap in out["laps"]] == [0, 1] and out["laps_total"] == 2
    assert out["stream_points"] > 0
    assert "raw_json" not in out["activity"]
    assert "as_of" in out
    assert size(out) < MAX_DEFAULT_CHARS


@pytest.mark.anyio
async def test_get_activity_by_garmin_id(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    out = await call(path, "get_activity", id=GID, id_kind="garmin")
    assert out["activity"]["id"] == act_id


@pytest.mark.anyio
async def test_get_activity_unknown_id(ride: tuple[Path, int]) -> None:
    path, _ = ride
    assert "no activity" in (await call(path, "get_activity", id=999999))["error"]
    assert "integer" in (await call(path, "get_activity", id="abc"))["error"]
    assert "no activity" in (await call(path, "get_activity", id="i1", id_kind="icu"))["error"]


@pytest.mark.anyio
async def test_get_activity_without_stream(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, conn = db
    gs.sync_activities(FakeGarmin([summary()], fit_ok=False), conn, tmp_path / "fit", D0, D1)
    out = await call(path, "get_activity", id=GID, id_kind="garmin")
    assert out["computed"] is None and "sync_now" in out["note"]


# analyze_segment


@pytest.mark.anyio
async def test_analyze_segment_time(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    out = await call(path, "analyze_segment", id=act_id, by="time", from_=0, to=60, above_w=150)
    seg = out["segment"]
    assert seg["avg_power"] == pytest.approx(200, abs=1)
    assert seg["pct_above_w"] == pytest.approx(100, abs=1)
    assert size(out) < MAX_DEFAULT_CHARS


@pytest.mark.anyio
async def test_analyze_segment_lap(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    seg = (await call(path, "analyze_segment", id=act_id, by="lap", from_=1, to=1))["segment"]
    assert seg["avg_power"] == pytest.approx(100, abs=1)


@pytest.mark.anyio
async def test_analyze_segment_errors(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    assert "error" in await call(path, "analyze_segment", id=act_id, by="speed", from_=0, to=10)
    assert "error" in await call(path, "analyze_segment", id=act_id, by="lap", from_=5, to=6)
    assert "no activity" in (await call(path, "analyze_segment", id=424242, by="time", from_=0, to=1))["error"]


# get_power_curve


@pytest.mark.anyio
async def test_power_curve(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    out = await call(path, "get_power_curve")
    by = {p["duration_s"]: p for p in out["curve"]}
    assert by[5]["watts"] == pytest.approx(200, abs=1) and by[5]["activity_id"] == act_id
    assert by[5]["date"] == "2026-11-14"
    assert by[7200]["watts"] is None
    assert size(out) < MAX_DEFAULT_CHARS
    picked = await call(path, "get_power_curve", durations=[60, 5])
    assert [p["duration_s"] for p in picked["curve"]] == [5, 60]


@pytest.mark.anyio
async def test_power_curve_errors(ride: tuple[Path, int]) -> None:
    path, _ = ride
    assert "unsupported durations [7]" in (await call(path, "get_power_curve", durations=[5, 7]))["error"]
    assert "error" in await call(path, "get_power_curve", start="2026-11-14", end="2026-11-01")
    assert "error" in await call(path, "get_power_curve", start="not-a-date")


@pytest.mark.anyio
async def test_power_curve_range_excludes(ride: tuple[Path, int]) -> None:
    path, _ = ride
    out = await call(path, "get_power_curve", start="2026-10-01", end="2026-10-31")
    assert all(p["watts"] is None for p in out["curve"])


# get_streams


@pytest.mark.anyio
async def test_streams_default(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    out = await call(path, "get_streams", id=act_id, fields=["power", "hr"])
    assert out["columns"] == ["t", "power", "hr"]
    assert out["points"][0][0] == 0 and out["points"][0][1] == 200
    assert out["truncated"] is False and out["next_start_s"] is None
    assert size(out) < MAX_DEFAULT_CHARS


@pytest.mark.anyio
async def test_streams_truncated_paging(ride: tuple[Path, int], monkeypatch: pytest.MonkeyPatch) -> None:
    path, act_id = ride
    monkeypatch.setattr(tools_ride, "MAX_POINTS", 5)
    out = await call(path, "get_streams", id=act_id, fields=["power"], resolution_s=1)
    assert len(out["points"]) == 5 and out["truncated"] is True
    nxt = out["next_start_s"]
    assert nxt == out["points"][-1][0] + 1
    page2 = await call(path, "get_streams", id=act_id, fields=["power"], resolution_s=1, start_s=nxt)
    assert page2["points"][0][0] == nxt


@pytest.mark.anyio
async def test_streams_max_points_at_1s(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    out = await call(path, "get_streams", id=act_id, fields=["power", "hr", "cadence"], resolution_s=1)
    assert len(out["points"]) <= tools_ride.MAX_POINTS
    assert size(out) < MAX_DEFAULT_CHARS


@pytest.mark.anyio
async def test_streams_window_and_errors(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    win = await call(path, "get_streams", id=act_id, fields=["power"], start_s=70, end_s=100)
    assert [p[0] for p in win["points"]] == [70, 80, 90] and win["points"][0][1] == 100
    assert "unknown fields" in (await call(path, "get_streams", id=act_id, fields=["watts"]))["error"]
    assert "error" in await call(path, "get_streams", id=act_id, fields=[])
    assert "error" in await call(path, "get_streams", id=act_id, fields=["power"], resolution_s=0)
    assert "no activity" in (await call(path, "get_streams", id=1234, fields=["power"]))["error"]


# get_athlete_settings


@pytest.mark.anyio
async def test_athlete_settings_profile_fallback(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    conn.execute("INSERT INTO athlete (id, name, ftp_w, weight_kg, updated_at) VALUES (1, 'L', 250, 72.5, 'x')")
    conn.commit()
    out = await call(path, "get_athlete_settings")
    assert out["settings"]["ftp_w"] == {"value": 250, "source": "athlete_profile"}
    assert out["settings"]["weight_kg"]["value"] == 72.5
    assert out["settings"]["lthr"] == {"value": None, "source": None}
    assert out["ftp_hint"] is None


@pytest.mark.anyio
async def test_athlete_settings_from_ride_and_stale_hint(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    conn = connect_write(path)
    conn.execute("UPDATE activity SET ftp_used = 150, lthr_used = 165, max_hr_used = 190 WHERE id = ?", (act_id,))
    conn.execute("INSERT OR REPLACE INTO activity_power_best (activity_id, duration_s, watts) VALUES (?, 1200, 200)",
                 (act_id,))
    conn.commit()
    conn.close()
    out = await call(path, "get_athlete_settings")
    ftp = out["settings"]["ftp_w"]
    assert ftp["value"] == 150 and ftp["source"] == "garmin_fit" and ftp["activity_id"] == act_id
    assert out["settings"]["lthr"]["value"] == 165
    assert out["best_20min_w_90d"] == 200 and "stale" in out["ftp_hint"]
    before = await call(path, "get_athlete_settings", date="2026-11-01")
    assert before["settings"]["ftp_w"]["source"] != "garmin_fit"
    assert "error" in await call(path, "get_athlete_settings", date="nope")



@pytest.mark.anyio
async def test_athlete_settings_manual_beats_fit_and_drives_hint(ride: tuple[Path, int]) -> None:
    path, act_id = ride
    conn = connect_write(path)
    conn.execute("UPDATE activity SET ftp_used = 150, weight_kg_used = 75 WHERE id = ?", (act_id,))
    conn.execute("INSERT OR REPLACE INTO activity_power_best (activity_id, duration_s, watts) VALUES (?, 1200, 200)",
                 (act_id,))
    thresholds.set_threshold(conn, kind="ftp_w", value=190, effective_from="2026-11-01")
    thresholds.set_threshold(conn, kind="weight_kg", value=72, effective_from="2026-11-01")
    thresholds.set_threshold(conn, kind="ftp_w", value=210, effective_from="2026-12-01")
    conn.commit()
    conn.close()
    out = await call(path, "get_athlete_settings")
    ftp = out["settings"]["ftp_w"]
    assert ftp["value"] == 190 and ftp["source"] == "manual" and ftp["effective_from"] == "2026-11-01"
    assert ftp["garmin_value"] == 150
    assert out["settings"]["weight_kg"]["value"] == 72 and out["settings"]["weight_kg"]["garmin_value"] == 75
    assert out["ftp_hint"] is None  # 200 * 0.95 = 190 is not above the manual 190
    early = await call(path, "get_athlete_settings", date="2026-10-31")
    assert early["settings"]["ftp_w"]["source"] != "manual"

# sync_now


@pytest.mark.anyio
async def test_sync_now_garmin(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, conn = db
    mcp = server(path, garmin_factory=lambda: FakeGarmin([summary()]), fit_dir=tmp_path / "fit")
    out = await call_on(mcp, "sync_now")
    assert out["source"] == "garmin" and out["new"] == 1 and out["fit_pending"] == 0
    assert conn.execute("SELECT count(*) FROM activity_stream").fetchone()[0] > 0


@pytest.mark.anyio
async def test_sync_now_fit_pending_hint(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, _ = db
    mcp = server(path, garmin_factory=lambda: FakeGarmin([summary()], fit_ok=False), fit_dir=tmp_path / "fit")
    out = await call_on(mcp, "sync_now")
    assert out["fit_pending"] == 1 and "FIT" in out["summary"]


@pytest.mark.anyio
async def test_sync_now_rate_limited(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, _ = db
    clock = Clock()
    mcp = server(path, clock, garmin_factory=lambda: FakeGarmin([]), fit_dir=tmp_path / "fit")
    assert "error" not in await call_on(mcp, "sync_now")
    clock.at = NOW + timedelta(seconds=30)
    assert "try again in 91 s" in (await call_on(mcp, "sync_now"))["error"]
    clock.at = NOW + timedelta(minutes=2)
    assert "error" not in await call_on(mcp, "sync_now")


@pytest.mark.anyio
async def test_sync_now_locked(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, conn = db
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO sync_state (key, value, updated_at) VALUES (?, 'other', ?)", (gs.LOCK_KEY, stamp))
    conn.commit()
    mcp = server(path, garmin_factory=lambda: FakeGarmin([summary()]), fit_dir=tmp_path / "fit")
    assert "already running" in (await call_on(mcp, "sync_now"))["error"]


class ClosingIntervals:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_sync_now_falls_back_to_intervals(db: tuple[Path, sqlite3.Connection], tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = db
    seen: list[Any] = []

    def fake_run(conn: sqlite3.Connection, client: Any, now: datetime) -> dict[str, Any]:
        seen.append(client)
        return {"wellness": 3, "activities": 2, "reconcile": {}}

    monkeypatch.setattr(tools_ride.intervals_job, "run", fake_run)

    def broken_garmin() -> Any:
        raise GarminError("GARMIN_PERSONAL_API_KEY missing")

    client = ClosingIntervals()
    mcp = server(path, garmin_factory=broken_garmin, intervals_factory=lambda: client, fit_dir=tmp_path / "fit")
    out = await call_on(mcp, "sync_now")
    assert out["source"] == "intervals" and out["activities"] == 2
    assert "Garmin unavailable" in out["fallback_reason"]
    assert seen == [client] and client.closed


@pytest.mark.anyio
async def test_sync_now_garmin_not_configured_falls_back(db: tuple[Path, sqlite3.Connection],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = db
    monkeypatch.setattr(tools_ride.intervals_job, "run", lambda c, cl, n: {"wellness": 0, "activities": 0})
    out = await call_on(server(path, intervals_factory=ClosingIntervals), "sync_now")
    assert out["source"] == "intervals" and out["fallback_reason"] == "Garmin is not configured"


@pytest.mark.anyio
async def test_sync_now_nothing_configured(db: tuple[Path, sqlite3.Connection]) -> None:
    path, _ = db
    assert "not configured either" in (await call_on(server(path), "sync_now"))["error"]
