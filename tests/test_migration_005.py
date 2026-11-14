"""Migration 005 (Garmin direct) and transactional migrations."""

from __future__ import annotations

import sqlite3

import pytest

from coach.db import migrate as m
from coach.db.migrate import connect, current_version, migrate


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _activity(conn):
    conn.execute(
        "INSERT INTO activity (id, garmin_activity_id, source, sport, sport_group, start_utc, updated_at)"
        " VALUES (1, 25074559291, 'intervals', 'road_biking', 'cycling', '2026-11-14T09:13:00+00:00', 'x')"
    )


def test_fresh_db_has_garmin_direct_schema(tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    migrate(conn)
    assert current_version(conn) >= 5
    assert {"intensity_factor", "tss", "work_kj", "ftp_used", "lthr_used", "max_hr_used",
            "weight_kg_used", "fit_path"} <= _cols(conn, "activity")
    assert {"activity_lap", "activity_zone_time"} <= _tables(conn)


def test_db_at_004_with_activity_migrates_intact(tmp_path, monkeypatch):
    conn = connect(tmp_path / "c.sqlite")
    real = m.migrations()
    monkeypatch.setattr(m, "migrations", lambda: [x for x in real if x[0] <= 4])
    migrate(conn)
    _activity(conn)
    conn.commit()
    monkeypatch.setattr(m, "migrations", lambda: real)
    assert migrate(conn) == ["005_garmin_direct.sql"]
    row = conn.execute("SELECT garmin_activity_id, source, tss, fit_path FROM activity").fetchone()
    assert row == (25074559291, "intervals", None, None)


def test_laps_and_zones_cascade_with_activity(tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    migrate(conn)
    _activity(conn)
    conn.execute("INSERT INTO activity_lap (activity_id, idx, start_utc) VALUES (1, 0, 't')")
    conn.execute("INSERT INTO activity_zone_time (activity_id, kind, zone, seconds) VALUES (1, 'hr', 2, 60)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO activity_zone_time (activity_id, kind, zone, seconds) VALUES (1, 'x', 1, 1)")
    conn.execute("DELETE FROM activity WHERE id = 1")
    assert conn.execute("SELECT count(*) FROM activity_lap").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM activity_zone_time").fetchone() == (0,)


def test_failing_migration_leaves_db_unchanged(tmp_path, monkeypatch):
    conn = connect(tmp_path / "c.sqlite")
    migrate(conn)
    before = current_version(conn)
    bad = tmp_path / f"{before + 1:03d}_bad.sql"
    bad.write_text("CREATE TABLE half_done (x INTEGER);\nALTER TABLE activity ADD COLUMN tss REAL;\n")
    real = m.migrations()
    monkeypatch.setattr(m, "migrations", lambda: [*real, (before + 1, bad)])
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert current_version(conn) == before
    assert "half_done" not in _tables(conn)
    assert not conn.in_transaction
