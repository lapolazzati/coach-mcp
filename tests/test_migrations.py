"""Migration 004 (personal software pivot)."""

from __future__ import annotations

import sqlite3

import pytest

from coach.db.migrate import connect, current_version, migrate, migrations


def _apply_upto(conn: sqlite3.Connection, version: int) -> None:
    """Run migrations <= version through the runner's bookkeeping."""
    from coach.db.migrate import applied

    applied(conn)
    for v, p in migrations():
        if v <= version:
            conn.executescript(p.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
                (v, p.name, "2026-11-01T00:00:00+00:00"),
            )
    conn.commit()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def db():
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


def test_fresh_db_reaches_004(db):
    assert current_version(db) >= 4
    assert {"note", "supplement", "supplement_intake", "readiness_day", "session_tombstone",
            "month_plan", "weekly_summary", "weekly_review"} <= _tables(db)
    assert not {"message_log", "pending_choice", "llm_call"} & _tables(db)
    assert {"ease_reason", "eased_from_json", "eased_at", "sync_dirty", "pinned_date",
            "row_version", "missing_polls", "start_time", "user_locked"} <= _cols(db, "planned_session")
    assert "client_ref" in _cols(db, "meal") and "client_ref" in _cols(db, "weight")


def test_db_at_003_with_history_migrates_intact(tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    _apply_upto(conn, 3)
    conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES ('2026-11-01', 82.1, 'intervals', 'x')")
    conn.execute(
        "INSERT INTO planned_session (external_id, date, slot, duration_min, kind, name, session_json, updated_at)"
        " VALUES ('coach:2026-11-07:am', '2026-11-07', 'am', 60, 'endurance', 'Z2', '{}', 'x')"
    )
    conn.execute("INSERT INTO message_log (ts_utc, local_date, direction, text) VALUES ('x', 'y', 'in', 'hi')")
    conn.commit()
    assert migrate(conn)[0] == "004_personal_software.sql"
    assert conn.execute("SELECT kg FROM weight").fetchall() == [(82.1,)]
    row = conn.execute("SELECT pinned_date, row_version, missing_polls, sync_dirty, user_locked"
                       " FROM planned_session").fetchone()
    assert row == (0, 0, 0, 0, 0)
    assert "message_log" not in _tables(conn)


def test_migrate_twice_is_noop(db):
    assert migrate(db) == []


def test_note_client_ref_unique(db):
    db.execute("INSERT INTO note (ts_utc, local_date, text, client_ref) VALUES ('t', 'd', 'a', 'r1')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO note (ts_utc, local_date, text, client_ref) VALUES ('t', 'd', 'b', 'r1')")


def test_weight_one_claude_row_per_day(db):
    ins = "INSERT INTO weight (date, kg, source, updated_at) VALUES ('2026-11-06', ?, ?, 'x')"
    db.execute(ins, (80.0, "claude"))
    db.execute(ins, (80.2, "intervals"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO weight (date, kg, source, updated_at, client_ref)"
                   " VALUES ('2026-11-06', 80.1, 'claude', 'y', 'other')")


def test_meal_and_weight_client_ref_unique_but_nullable(db):
    ins = "INSERT INTO meal (ts_utc, local_date, source, client_ref) VALUES ('t', 'd', 'photo', ?)"
    db.execute(ins, (None,))
    db.execute(ins, (None,))
    db.execute(ins, ("m1",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(ins, ("m1",))


def test_supplement_timing_checked(db):
    db.execute("INSERT INTO supplement (name, timing) VALUES ('creatine', 'morning')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO supplement (name, timing) VALUES ('x', 'midnight')")


def test_supplement_intake_fk(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO supplement_intake (supplement_id, local_date, ts_utc) VALUES (999, 'd', 't')")
