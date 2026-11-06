"""reconcile(): push planned_session to intervals.icu and pull back deletions (U5, KTD10)."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime
from typing import Any

import httpx
import pytest
from store_helpers import TODAY, add_session, count, fresh_db, session

from coach.integrations.intervals import IntervalsClient, IntervalsError
from coach.plan.reconcile import reconcile
from coach.store import plan_ops, sync
from coach.store.db import TZ

NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 6, 0, tzinfo=TZ)
SAT, SUN, MON, TUE = "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"
SS_TEXT = "Warmup\n- 15m ramp 50-75%\n\nMain set 3x\n- 12m 88-93%\n- 5m 55%\n\nCooldown\n- 10m 50%"


class FakeClient:
    """In-memory intervals.icu: records calls, can fail on demand, has a mid-upsert hook."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail_upsert = False
        self.fail_delete = False
        self.on_upsert: Callable[[], None] | None = None
        self._next_id = 1000

    def list_events(self, oldest: date, newest: date, category: str = "WORKOUT") -> list[dict[str, Any]]:
        self.calls.append(("list", (oldest, newest)))
        return [copy.deepcopy(e) for e in self.events.values()
                if oldest.isoformat() <= e["start_date_local"][:10] <= newest.isoformat()]

    def upsert_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append(("upsert", [e["external_id"] for e in events]))
        if self.on_upsert:
            self.on_upsert()
        if self.fail_upsert:
            raise IntervalsError("POST events/bulk -> 500: boom")
        out = []
        for e in events:
            ev = copy.deepcopy(e)
            old = self.events.get(e["external_id"])
            ev["id"] = old["id"] if old else self._new_id()
            self.events[e["external_id"]] = ev
            out.append(copy.deepcopy(ev))
        return out

    def delete_events(self, external_ids: list[str]) -> None:
        self.calls.append(("delete", sorted(external_ids)))
        if self.fail_delete:
            raise IntervalsError("PUT events/bulk-delete -> 500: boom")
        for x in external_ids:
            self.events.pop(x, None)

    def writes(self) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] != "list"]

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def run(conn: sqlite3.Connection, client: Any) -> dict[str, Any]:
    return reconcile(conn, client, now=NOW)


def _json(steps_text: str) -> str:
    return json.dumps({"steps_text": steps_text})


def test_three_new_sessions_go_in_one_bulk_upsert(conn, client):
    a = add_session(conn, SAT, session_json=_json(SS_TEXT), duration_min=70)
    b = add_session(conn, SUN, "pm", sport="run", session_json=_json("- 45m Z2 HR"), duration_min=45)
    c = add_session(conn, MON, sport="strength", session_json=_json(""), duration_min=40, name="Gym")
    summary = run(conn, client)
    assert client.writes() == [("upsert", [a, b, c])]
    assert summary["pushed"] == 3 and summary["errors"] == []
    ev = client.events[a]
    assert ev["start_date_local"] == f"{SAT}T07:00:00" and ev["type"] == "Ride"
    assert "Main set 3x\n- 12m 88-93%" in ev["description"]
    assert client.events[b]["start_date_local"] == f"{SUN}T18:00:00" and client.events[b]["type"] == "Run"
    assert client.events[c]["type"] == "WeightTraining" and client.events[c]["moving_time"] == 2400
    for ext in (a, b, c):
        row = session(conn, ext)
        assert row and row["sync_dirty"] == 0 and row["last_written_hash"] and row["icu_event_id"]


def test_start_time_is_sent_when_set(conn, client):
    ext = add_session(conn, SAT, start_time="06:15", sport="virtual_ride")
    run(conn, client)
    assert client.events[ext]["start_date_local"] == f"{SAT}T06:15:00"
    assert client.events[ext]["type"] == "VirtualRide"


def test_second_run_makes_no_write_calls(conn, client):
    add_session(conn, SAT, session_json=_json(SS_TEXT))
    add_session(conn, SUN, "pm")
    run(conn, client)
    client.calls.clear()
    summary = run(conn, client)
    assert client.writes() == []
    assert summary["pushed"] == 0 and summary["deleted"] == 0


def test_rest_day_is_never_pushed(conn, client):
    add_session(conn, SAT, sport="rest", name="Rest")
    run(conn, client)
    assert client.writes() == []


def test_move_is_one_delete_plus_one_upsert(conn, client):
    old = add_session(conn, TUE)
    run(conn, client)
    client.calls.clear()
    res = plan_ops.move_session(conn, old, "2026-11-11", today=TODAY)
    new = res.row["external_id"]  # type: ignore[index]
    summary = run(conn, client)
    assert client.writes() == [("delete", [old]), ("upsert", [new])]
    assert old not in client.events and new in client.events
    assert count(conn, "SELECT 1 FROM session_tombstone WHERE pending_delete = 1") == 0
    assert summary["deleted"] == 1 and summary["pushed"] == 1


def test_user_edit_in_intervals_is_logged_and_overwritten(conn, client, caplog):
    ext = add_session(conn, SAT, name="Sweet spot", session_json=_json(SS_TEXT))
    run(conn, client)
    client.events[ext]["name"] = "Lapo's version"
    client.calls.clear()
    with caplog.at_level(logging.WARNING):
        summary = run(conn, client)
    assert client.writes() == [("upsert", [ext])]
    assert client.events[ext]["name"] == "Sweet spot"
    assert summary["overwritten"] == 1
    assert any(ext in r.getMessage() for r in caplog.records)
    assert session(conn, ext)["user_locked"] == 0  # type: ignore[index]


def test_http_500_leaves_row_dirty(conn):
    ext = add_session(conn, SAT)
    sync.mark_dirty(conn, int(session(conn, ext)["id"]))  # type: ignore[index, arg-type]
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(500, text="boom")

    real = IntervalsClient("k", "i1", transport=httpx.MockTransport(handler), retries=1, backoff_s=0)
    summary = reconcile(conn, real, now=NOW)
    assert summary["errors"] and summary["pushed"] == 0
    row = session(conn, ext)
    assert row and row["sync_dirty"] == 1 and row["last_written_hash"] is None


def test_edit_during_push_keeps_row_dirty(conn, client):
    ext = add_session(conn, SAT)
    sid = int(session(conn, ext)["id"])  # type: ignore[index, arg-type]

    def edit() -> None:
        sync.mark_dirty(conn, sid)
        conn.commit()

    client.on_upsert = edit
    run(conn, client)
    assert session(conn, ext)["sync_dirty"] == 1  # type: ignore[index]
    client.on_upsert = None
    run(conn, client)
    assert session(conn, ext)["sync_dirty"] == 0  # type: ignore[index]


def test_failed_tombstone_delete_stays_pending_and_retries(conn, client):
    old = add_session(conn, TUE)
    run(conn, client)
    new = plan_ops.move_session(conn, old, "2026-11-11", today=TODAY).row["external_id"]  # type: ignore[index]
    client.fail_delete = True
    summary = run(conn, client)
    assert summary["errors"] and new in client.events and old in client.events
    assert count(conn, "SELECT 1 FROM session_tombstone WHERE external_id = ? AND pending_delete = 1", (old,)) == 1
    client.fail_delete = False
    client.calls.clear()
    run(conn, client)
    assert client.writes() == [("delete", [old])]
    assert count(conn, "SELECT 1 FROM session_tombstone WHERE pending_delete = 1") == 0


def test_dirty_row_missing_remotely_is_pushed_not_counted(conn, client):
    ext = add_session(conn, SAT)
    run(conn, client)
    del client.events[ext]
    sync.mark_dirty(conn, int(session(conn, ext)["id"]))  # type: ignore[index, arg-type]
    conn.commit()
    summary = run(conn, client)
    assert ext in client.events and summary["pushed"] == 1 and summary["missing"] == 0
    assert session(conn, ext)["missing_polls"] == 0  # type: ignore[index]


def test_clean_row_missing_twice_is_treated_as_deleted_by_lapo(conn, client):
    ext = add_session(conn, SAT)
    run(conn, client)
    del client.events[ext]
    client.calls.clear()
    first = run(conn, client)
    row = session(conn, ext)
    assert first["missing"] == 1 and row["missing_polls"] == 1 and row["status"] == "planned"  # type: ignore[index]
    assert client.writes() == []
    second = run(conn, client)
    row = session(conn, ext)
    assert second["treated_deleted"] == [ext] and row["status"] == "skipped"  # type: ignore[index]
    assert count(conn, "SELECT 1 FROM audit WHERE actor = 'job:reconcile' AND action = 'skip_session'") == 1
    assert client.writes() == []
    run(conn, client)
    assert client.writes() == [] and session(conn, ext)["sync_dirty"] == 0  # type: ignore[index]


def test_event_back_resets_missing_polls(conn, client):
    ext = add_session(conn, SAT)
    run(conn, client)
    saved = client.events.pop(ext)
    run(conn, client)
    client.events[ext] = saved
    run(conn, client)
    assert session(conn, ext)["missing_polls"] == 0  # type: ignore[index]


def test_orphan_coach_event_is_deleted(conn, client):
    client.events["coach:2026-11-08:pm"] = {"id": 1, "external_id": "coach:2026-11-08:pm",
                                            "start_date_local": f"{SUN}T18:00:00", "name": "stale"}
    client.events["garmin:42"] = {"id": 2, "external_id": "garmin:42", "start_date_local": f"{SUN}T09:00:00"}
    client.events["noid"] = {"id": 3, "external_id": None, "start_date_local": f"{SUN}T10:00:00"}
    summary = run(conn, client)
    assert client.writes() == [("delete", ["coach:2026-11-08:pm"])]
    assert summary["orphans"] == 1 and "garmin:42" in client.events and "noid" in client.events


def test_skipped_dirty_session_deletes_remote_event(conn, client):
    ext = add_session(conn, SAT)
    run(conn, client)
    plan_ops.skip_session(conn, ext, reason="tired")
    client.calls.clear()
    summary = run(conn, client)
    assert client.writes() == [("delete", [ext])] and ext not in client.events
    row = session(conn, ext)
    assert row and row["external_id"] == ext and row["sync_dirty"] == 0 and summary["deleted"] == 1
    assert count(conn, "SELECT 1 FROM session_tombstone") == 0
    client.calls.clear()
    run(conn, client)
    assert client.writes() == []


def test_window_skips_far_clean_rows_but_pushes_far_dirty_ones(conn, client):
    far = add_session(conn, "2026-12-20")
    run(conn, client)
    assert client.writes() == []
    sync.mark_dirty(conn, int(session(conn, far)["id"]))  # type: ignore[index, arg-type]
    conn.commit()
    run(conn, client)
    assert client.writes() == [("upsert", [far])]


def test_past_rows_are_left_alone(conn, client):
    add_session(conn, "2026-11-05")
    run(conn, client)
    assert client.writes() == []
