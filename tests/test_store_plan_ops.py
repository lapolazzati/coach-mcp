"""Store layer: plan ops, audit and undo (U4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest
from store_helpers import TODAY, add_session, count, fresh_db, session

from coach.store import derived, plan_ops, sync

TUE, WED, THU = "2026-11-10", "2026-11-11", "2026-11-12"
EASY = {"kind": "endurance", "name": "60' Z2", "duration_min": 60, "session_json": {"steps": ["60m Z2"]}}


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


def _audit(conn: sqlite3.Connection, aid: int | None) -> dict[str, object]:
    cur = conn.execute("SELECT * FROM audit WHERE id = ?", (aid,))
    row = cur.fetchone()
    assert row is not None
    return {d[0]: v for d, v in zip(cur.description, row, strict=True)}


def _tombstones(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return conn.execute("SELECT external_id, pending_delete FROM session_tombstone ORDER BY external_id").fetchall()


# --- move ------------------------------------------------------------------


def test_move_tue_to_wed(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, WED, today=TODAY)
    assert res.status == "ok" and res.row is not None
    row = res.row
    assert row["date"] == WED and row["external_id"] == f"coach:{WED}:am"
    assert row["sync_dirty"] == 1 and row["pinned_date"] == 1 and row["user_locked"] == 0
    assert row["row_version"] >= 1
    assert session(conn, old) is None
    assert _tombstones(conn) == [(old, 1)]
    entry = _audit(conn, res.audit_id)
    assert entry["action"] == "move_session" and entry["actor"] == "claude"
    assert json.loads(str(entry["before_json"]))["external_id"] == old
    assert json.loads(str(entry["after_json"]))["external_id"] == row["external_id"]


def test_move_with_lock_sets_user_locked(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, WED, "pm", lock=True, today=TODAY)
    assert res.row is not None and res.row["user_locked"] == 1 and res.row["slot"] == "pm"
    assert res.row["external_id"] == f"coach:{WED}:pm"


def test_move_keeps_existing_lock(conn):
    old = add_session(conn, TUE, user_locked=1)
    res = plan_ops.move_session(conn, old, WED, today=TODAY)
    assert res.row is not None and res.row["user_locked"] == 1


def test_move_into_taken_slot_refused_with_free_slots(conn):
    old = add_session(conn, TUE)
    add_session(conn, WED, "am", name="VO2")
    res = plan_ops.move_session(conn, old, WED, "am", today=TODAY)
    assert res.status == "refused" and "VO2" in res.message
    assert {"date": WED, "slot": "pm"} in res.free_slots
    assert {"date": WED, "slot": "am"} not in res.free_slots
    assert session(conn, old) is not None and count(conn, "SELECT * FROM audit") == 0


def test_move_to_same_slot_refused(conn):
    old = add_session(conn, TUE)
    assert plan_ops.move_session(conn, old, TUE, today=TODAY).status == "refused"


def test_move_to_past_refused(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, "2026-11-05", today=TODAY)
    assert res.status == "refused" and "past" in res.message


def test_move_done_session_refused(conn):
    old = add_session(conn, TUE, status="done")
    assert plan_ops.move_session(conn, old, WED, today=TODAY).status == "refused"


def test_move_unknown_session(conn):
    assert plan_ops.move_session(conn, "coach:2026-11-10:am", WED, today=TODAY).status == "not_found"


def test_move_bad_slot_raises(conn):
    old = add_session(conn, TUE)
    with pytest.raises(ValueError):
        plan_ops.move_session(conn, old, WED, "noon", today=TODAY)
    assert session(conn, old) is not None


def test_job_cannot_move_locked_session(conn):
    old = add_session(conn, TUE, user_locked=1)
    res = plan_ops.move_session(conn, old, WED, actor="job:reconcile", today=TODAY)
    assert res.status == "refused" and "locked" in res.message
    assert session(conn, old) is not None


def test_job_can_move_unlocked_session(conn):
    old = add_session(conn, TUE)
    assert plan_ops.move_session(conn, old, WED, actor="job", today=TODAY).status == "ok"


def test_move_back_clears_tombstone(conn):
    old = add_session(conn, TUE)
    plan_ops.move_session(conn, old, WED, today=TODAY)
    plan_ops.move_session(conn, f"coach:{WED}:am", TUE, today=TODAY)
    assert _tombstones(conn) == [(f"coach:{WED}:am", 1)]
    assert session(conn, old) is not None


# --- undo ------------------------------------------------------------------


def test_undo_move_restores_and_marks_undone(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, WED, today=TODAY)
    assert res.audit_id is not None
    out = plan_ops.undo(conn, res.audit_id)
    assert out.status == "undone", out.message
    row = session(conn, old)
    assert row is not None and row["date"] == TUE and row["pinned_date"] == 0
    assert row["sync_dirty"] == 1
    assert session(conn, f"coach:{WED}:am") is None
    assert _tombstones(conn) == [(f"coach:{WED}:am", 1)]
    assert _audit(conn, res.audit_id)["undone"] == 1
    assert count(conn, "SELECT * FROM audit WHERE action = 'undo:move_session'") == 1


def test_undo_twice_refused(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, WED, today=TODAY)
    assert res.audit_id is not None
    plan_ops.undo(conn, res.audit_id)
    again = plan_ops.undo(conn, res.audit_id)
    assert again.status == "refused" and "already" in again.message


def test_undo_unknown_audit(conn):
    assert plan_ops.undo(conn, 999).status == "not_found"


def test_undo_after_later_change_refused(conn):
    old = add_session(conn, TUE)
    moved = plan_ops.move_session(conn, old, WED, today=TODAY)
    eased = plan_ops.ease_session(conn, f"coach:{WED}:am", eased=EASY, reason="HRV low", actor="claude")
    assert moved.audit_id is not None and eased.status == "ok"
    out = plan_ops.undo(conn, moved.audit_id)
    assert out.status == "refused"
    assert session(conn, f"coach:{WED}:am") is not None
    assert _audit(conn, moved.audit_id)["undone"] == 0
    # undoing the newest change first, then the move, works
    assert eased.audit_id is not None and plan_ops.undo(conn, eased.audit_id).status == "undone"
    assert plan_ops.undo(conn, moved.audit_id).status == "undone"
    assert session(conn, old) is not None


def test_undo_move_refused_when_old_slot_retaken(conn):
    old = add_session(conn, TUE)
    moved = plan_ops.move_session(conn, old, WED, today=TODAY)
    add_session(conn, TUE, name="new session")
    assert moved.audit_id is not None
    out = plan_ops.undo(conn, moved.audit_id)
    assert out.status == "refused"
    assert session(conn, f"coach:{WED}:am") is not None


def test_undo_after_push_still_works(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, WED, today=TODAY)
    assert res.row is not None and res.audit_id is not None
    pushed = sync.mark_pushed(conn, int(res.row["id"]), int(res.row["row_version"]), "hash", icu_event_id=42)
    assert pushed
    assert plan_ops.undo(conn, res.audit_id).status == "undone"
    row = session(conn, old)
    assert row is not None and row["sync_dirty"] == 1 and row["icu_event_id"] is None


# --- skip ------------------------------------------------------------------


def test_skip_and_undo(conn):
    ext = add_session(conn, TUE)
    res = plan_ops.skip_session(conn, ext, reason="travel")
    assert res.status == "ok" and res.row is not None
    assert res.row["status"] == "skipped" and res.row["sync_dirty"] == 1
    assert plan_ops.skip_session(conn, ext).status == "refused"
    assert res.audit_id is not None and plan_ops.undo(conn, res.audit_id).status == "undone"
    row = session(conn, ext)
    assert row is not None and row["status"] == "planned"


def test_skip_unknown_and_job_on_locked(conn):
    assert plan_ops.skip_session(conn, "coach:2026-11-10:pm").status == "not_found"
    ext = add_session(conn, TUE, user_locked=1)
    assert plan_ops.skip_session(conn, ext, actor="job").status == "refused"


# --- swap ------------------------------------------------------------------


def test_swap_and_undo(conn):
    a = add_session(conn, TUE, name="threshold")
    b = add_session(conn, THU, "pm", name="endurance", start_time="18:30")
    res = plan_ops.swap_sessions(conn, a, b, today=TODAY)
    assert res.status == "ok" and len(res.rows) == 2
    on_tue, on_thu = session(conn, a), session(conn, b)
    assert on_tue is not None and on_thu is not None
    assert on_tue["name"] == "endurance" and on_thu["name"] == "threshold"
    assert on_tue["pinned_date"] == 1 and on_thu["pinned_date"] == 1
    assert on_tue["start_time"] is None and on_thu["start_time"] == "18:30"
    assert _tombstones(conn) == []  # both ids are still in use
    assert res.audit_id is not None and plan_ops.undo(conn, res.audit_id).status == "undone"
    back_a, back_b = session(conn, a), session(conn, b)
    assert back_a is not None and back_b is not None
    assert back_a["name"] == "threshold" and back_b["name"] == "endurance"


def test_swap_refusals(conn):
    a = add_session(conn, TUE)
    past = add_session(conn, "2026-11-04")
    locked = add_session(conn, THU, user_locked=1)
    assert plan_ops.swap_sessions(conn, a, a, today=TODAY).status == "refused"
    assert plan_ops.swap_sessions(conn, a, past, today=TODAY).status == "refused"
    assert plan_ops.swap_sessions(conn, a, locked, actor="job", today=TODAY).status == "refused"
    assert plan_ops.swap_sessions(conn, a, "coach:2026-11-20:am", today=TODAY).status == "not_found"
    assert count(conn, "SELECT * FROM audit") == 0


# --- ease ------------------------------------------------------------------


def test_ease_keeps_first_original(conn):
    ext = add_session(conn, TUE)
    first = plan_ops.ease_session(conn, ext, eased=EASY, reason="HRV low", actor="job:auto_ease")
    assert first.status == "ok" and first.row is not None
    assert first.row["kind"] == "endurance" and first.row["ease_reason"] == "HRV low"
    original = json.loads(str(first.row["eased_from_json"]))
    assert original["kind"] == "threshold"
    second = plan_ops.ease_session(conn, ext, eased={"duration_min": 45}, reason="still low", actor="claude")
    assert second.row is not None and json.loads(str(second.row["eased_from_json"]))["kind"] == "threshold"


def test_job_ease_on_locked_records_advice_only(conn):
    ext = add_session(conn, TUE, user_locked=1)
    before = session(conn, ext)
    res = plan_ops.ease_session(conn, ext, eased=EASY, reason="HRV low", actor="job:auto_ease")
    assert res.status == "advice"
    assert session(conn, ext) == before
    entry = _audit(conn, res.audit_id)
    assert entry["action"] == "ease_advice" and entry["before_json"] is None
    assert json.loads(str(entry["after_json"]))["proposed"]["kind"] == "endurance"
    assert res.audit_id is not None and plan_ops.undo(conn, res.audit_id).status == "refused"


def test_job_ease_on_pinned_unlocked_is_applied(conn):
    ext = add_session(conn, TUE, pinned_date=1, user_locked=0)
    res = plan_ops.ease_session(conn, ext, eased=EASY, reason="HRV low", actor="job")
    assert res.status == "ok" and res.row is not None and res.row["kind"] == "endurance"


def test_claude_ease_on_locked_is_applied(conn):
    ext = add_session(conn, TUE, user_locked=1)
    assert plan_ops.ease_session(conn, ext, eased=EASY, reason="Lapo asked", actor="claude").status == "ok"


def test_ease_errors(conn):
    ext = add_session(conn, TUE, status="done")
    with pytest.raises(ValueError):
        plan_ops.ease_session(conn, ext, eased={"ftp": 300}, reason="x", actor="claude")
    with pytest.raises(ValueError):
        plan_ops.ease_session(conn, ext, eased={}, reason="x", actor="claude")
    assert plan_ops.ease_session(conn, ext, eased=EASY, reason="x", actor="claude").status == "refused"
    assert plan_ops.ease_session(conn, "coach:2026-11-30:am", eased=EASY, reason="x",
                                 actor="claude").status == "not_found"


def test_undo_ease_restores_content(conn):
    ext = add_session(conn, TUE)
    res = plan_ops.ease_session(conn, ext, eased=EASY, reason="HRV low", actor="claude")
    assert res.audit_id is not None and plan_ops.undo(conn, res.audit_id).status == "undone"
    row = session(conn, ext)
    assert row is not None and row["kind"] == "threshold" and row["eased_from_json"] is None


# --- sync bookkeeping ------------------------------------------------------


def test_mark_pushed_with_stale_version_stays_dirty(conn):
    ext = add_session(conn, TUE)
    res = plan_ops.skip_session(conn, ext)
    assert res.row is not None
    sid, version = int(res.row["id"]), int(res.row["row_version"])
    sync.mark_dirty(conn, sid)  # an edit lands while the push is in flight
    conn.commit()
    assert sync.mark_pushed(conn, sid, version, "h") is False
    row = session(conn, ext)
    assert row is not None and row["sync_dirty"] == 1
    assert sync.mark_pushed(conn, sid, version + 1, "h") is True
    assert sync.dirty_sessions(conn) == []


def test_missing_polls_count_only_clean_pushed_rows(conn):
    ext = add_session(conn, TUE)
    row = session(conn, ext)
    assert row is not None
    sid = int(str(row["id"]))
    assert sync.record_missing_poll(conn, sid) == 0  # never pushed
    sync.mark_pushed(conn, sid, int(str(row["row_version"])), "h")
    assert sync.record_missing_poll(conn, sid) == 1
    assert sync.record_missing_poll(conn, sid) == 2
    sync.reset_missing_polls(conn, sid)
    assert sync.record_missing_poll(conn, sid) == 1


def test_confirm_tombstone(conn):
    old = add_session(conn, TUE)
    res = plan_ops.move_session(conn, old, WED, today=TODAY)
    assert res.row is not None
    assert [t["external_id"] for t in sync.pending_tombstones(conn)] == [old]
    sync.confirm_tombstone(conn, old, int(res.row["id"]))
    assert sync.pending_tombstones(conn) == []


def test_external_id_format():
    assert sync.external_id("2026-11-10", "pm") == "coach:2026-11-10:pm"
    with pytest.raises(ValueError):
        sync.external_id("2026-11-10", "noon")


# --- derived ---------------------------------------------------------------


def test_mark_sleep_seen_only_when_null(conn):
    assert derived.mark_sleep_seen(conn, "2026-11-06", "2026-11-06T05:10:00Z") is True
    assert derived.mark_sleep_seen(conn, "2026-11-06", "2026-11-06T07:00:00Z") is False
    row = derived.get_readiness(conn, "2026-11-06")
    assert row is not None and row["sleep_seen_at"] == "2026-11-06T05:10:00Z"


def test_upsert_readiness_keeps_final_and_sleep_seen(conn):
    derived.mark_sleep_seen(conn, "2026-11-06", "2026-11-06T05:10:00Z")
    assert derived.upsert_readiness(conn, "2026-11-06", status="amber", flags=["hrv_low"], signals={"hrv": 41},
                                    final=True)
    assert not derived.upsert_readiness(conn, "2026-11-06", status="green", flags=[], signals={})
    row = derived.get_readiness(conn, "2026-11-06")
    assert row is not None and row["status"] == "amber" and row["final"] == 1
    assert row["sleep_seen_at"] == "2026-11-06T05:10:00Z"
    with pytest.raises(ValueError):
        derived.upsert_readiness(conn, "2026-11-07", status="purple", flags=[], signals={})


def test_upsert_weekly_summary(conn):
    derived.upsert_weekly_summary(conn, "2026-11-02", {"tss": 400})
    derived.upsert_weekly_summary(conn, "2026-11-02", {"tss": 420})
    assert conn.execute("SELECT metrics_json FROM weekly_summary").fetchall() == [('{"tss": 420}',)]
    with pytest.raises(ValueError):
        derived.upsert_weekly_summary(conn, "2026-11-03", {})
