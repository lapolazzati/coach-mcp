"""U1: manual athlete thresholds (migration 006, coach.store.thresholds)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from coach.db import migrate as m
from coach.db.migrate import connect, migrate
from coach.store import audit, thresholds
from tests.store_helpers import fresh_db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


def test_migration_006_applies_on_top_of_005(tmp_path, monkeypatch):
    c = connect(tmp_path / "c.sqlite")
    real = m.migrations()
    monkeypatch.setattr(m, "migrations", lambda: [x for x in real if x[0] <= 5])
    migrate(c)
    monkeypatch.setattr(m, "migrations", lambda: real)
    assert migrate(c) == ["006_athlete_threshold.sql"]
    cols = {r[1] for r in c.execute("PRAGMA table_info(athlete_threshold)")}
    assert cols == {"id", "kind", "value", "effective_from", "note", "created_at", "updated_at"}


def test_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO athlete_threshold (kind, value, effective_from, created_at, updated_at)"
                     " VALUES ('lthr', 170, '2026-11-01', 'x', 'x')")
    conn.execute("INSERT INTO athlete_threshold (kind, value, effective_from, created_at, updated_at)"
                 " VALUES ('ftp_w', 190, '2026-11-01', 'x', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO athlete_threshold (kind, value, effective_from, created_at, updated_at)"
                     " VALUES ('ftp_w', 200, '2026-11-01', 'x', 'x')")


def test_upsert_keeps_row_id_and_audits(conn):
    first = thresholds.set_threshold(conn, kind="ftp_w", value=185, effective_from="2026-11-01")
    second = thresholds.set_threshold(conn, kind="ftp_w", value=190, effective_from="2026-11-01", note="coach")
    assert first.status == "created" and second.status == "replaced"
    assert first.row and second.row and first.row["id"] == second.row["id"]
    assert second.row["value"] == 190 and second.previous and second.previous["value"] == 185
    assert second.audit_id is not None


def test_undo_restores_then_deletes(conn):
    first = thresholds.set_threshold(conn, kind="ftp_w", value=185, effective_from="2026-11-01")
    second = thresholds.set_threshold(conn, kind="ftp_w", value=190, effective_from="2026-11-01")
    assert second.audit_id and first.audit_id
    assert audit.undo(conn, second.audit_id).status == "undone"
    assert thresholds.effective(conn, "2026-11-10")["ftp_w"]["value"] == 185
    assert audit.undo(conn, first.audit_id).status == "undone"
    assert thresholds.effective(conn, "2026-11-10") == {}


@pytest.mark.parametrize(("kind", "value", "match"), [
    ("ftp_w", 79, "ftp_w"), ("ftp_w", 501, "ftp_w"), ("weight_kg", 39, "weight_kg"),
    ("weight_kg", 151, "weight_kg"), ("lthr", 170, "kind"),
])
def test_validation(conn, kind, value, match):
    with pytest.raises(ValueError, match=match):
        thresholds.set_threshold(conn, kind=kind, value=value, effective_from="2026-11-01")


def test_effective_picks_latest_on_or_before_day(conn):
    thresholds.set_threshold(conn, kind="ftp_w", value=180, effective_from="2026-10-01")
    thresholds.set_threshold(conn, kind="ftp_w", value=190, effective_from="2026-11-10")
    thresholds.set_threshold(conn, kind="weight_kg", value=72.5, effective_from="2026-11-01")
    assert thresholds.effective(conn, "2026-09-30") == {}
    assert thresholds.effective(conn, "2026-11-09")["ftp_w"]["value"] == 180
    eff = thresholds.effective(conn, "2026-11-10")
    assert eff["ftp_w"]["value"] == 190 and eff["weight_kg"]["value"] == 72.5


def test_effective_tolerates_missing_table():
    c = sqlite3.connect(":memory:")
    assert thresholds.effective(c, "2026-11-10") == {}
