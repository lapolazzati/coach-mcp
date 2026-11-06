"""Store layer: logging writes (U4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest
from store_helpers import count, fresh_db

from coach.store import audit, logs, plan_ops


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    yield from fresh_db()


def _audits(conn: sqlite3.Connection, entity: str) -> list[tuple[str, str, str | None, str | None]]:
    return conn.execute("SELECT actor, action, before_json, after_json FROM audit WHERE entity = ? ORDER BY id",
                        (entity,)).fetchall()


def _seed_weights(conn: sqlite3.Connection, kgs: dict[str, float]) -> None:
    for day, kg in kgs.items():
        conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES (?, ?, 'intervals', 'x')", (day, kg))
    conn.commit()


WEEK = {f"2026-11-0{d}": 80.0 for d in range(1, 6)}


# --- meals -----------------------------------------------------------------


def test_log_meal_stores_row_and_audit(conn):
    res = logs.log_meal(conn, description="pasta al pomodoro", kcal=650, carbs_g=110, protein_g=20, fat_g=12,
                        ts_utc="2026-11-06T11:30:00Z", client_ref="m1")
    assert res.status == "created" and res.row is not None
    assert res.row["local_date"] == "2026-11-06" and res.row["kcal"] == 650 and res.row["source"] == "photo"
    rows = _audits(conn, "meal")
    assert len(rows) == 1
    actor, action, before, after = rows[0]
    assert (actor, action, before) == ("claude", "log_meal", None)
    assert json.loads(after or "{}")["id"] == res.row["id"]
    assert res.audit_id is not None


def test_meal_local_date_is_rome(conn):
    res = logs.log_meal(conn, description="late snack", kcal=200, ts_utc="2026-11-06T23:30:00Z")
    assert res.row is not None and res.row["local_date"] == "2026-11-07"


def test_same_client_ref_twice_is_one_row(conn):
    first = logs.log_meal(conn, description="toast", kcal=300, client_ref="dup")
    second = logs.log_meal(conn, description="toast", kcal=300, client_ref="dup")
    assert first.status == "created" and second.status == "existing"
    assert second.row is not None and first.row is not None and second.row["id"] == first.row["id"]
    assert count(conn, "SELECT * FROM meal") == 1
    assert count(conn, "SELECT * FROM audit") == 1


def test_meal_without_client_ref_logs_twice(conn):
    logs.log_meal(conn, description="espresso", kcal=5)
    logs.log_meal(conn, description="espresso", kcal=5)
    assert count(conn, "SELECT * FROM meal") == 2


@pytest.mark.parametrize("kwargs", [{"kcal": -1}, {"kcal": 100, "source": "telegram"},
                                    {"kcal": 100, "confidence": 1.5}])
def test_meal_rejects_bad_values(conn, kwargs):
    with pytest.raises(ValueError):
        logs.log_meal(conn, description="x", **kwargs)
    assert count(conn, "SELECT * FROM meal") == 0


def test_bad_actor_rolls_back(conn):
    with pytest.raises(ValueError):
        logs.log_meal(conn, description="x", kcal=1, actor="mallory")
    assert count(conn, "SELECT * FROM meal") == 0 and count(conn, "SELECT * FROM audit") == 0


def test_undo_meal_deletes_it(conn):
    res = logs.log_meal(conn, description="gelato", kcal=300)
    assert res.audit_id is not None
    out = plan_ops.undo(conn, res.audit_id)
    assert out.status == "undone"
    assert count(conn, "SELECT * FROM meal") == 0
    assert conn.execute("SELECT undone FROM audit WHERE id = ?", (res.audit_id,)).fetchone() == (1,)


# --- weight ----------------------------------------------------------------


def test_weight_far_from_mean_needs_confirmation(conn):
    _seed_weights(conn, WEEK)
    res = logs.log_weight(conn, kg=83.0, local_date="2026-11-06")
    assert res.status == "needs_confirmation" and "implausible" in res.flags
    assert count(conn, "SELECT * FROM weight WHERE source = 'claude'") == 0
    assert count(conn, "SELECT * FROM audit") == 0


def test_weight_far_from_mean_with_confirm_is_stored(conn):
    _seed_weights(conn, WEEK)
    res = logs.log_weight(conn, kg=83.0, local_date="2026-11-06", confirm=True)
    assert res.status == "created" and "implausible" in res.flags
    assert count(conn, "SELECT * FROM weight WHERE source = 'claude'") == 1


def test_weight_within_range_of_mean_is_stored(conn):
    _seed_weights(conn, WEEK)
    res = logs.log_weight(conn, kg=81.5, local_date="2026-11-06")
    assert res.status == "created" and res.flags == []


@pytest.mark.parametrize("kg", [45.0, 151.0])
def test_weight_outside_absolute_range_needs_confirmation(conn, kg):
    res = logs.log_weight(conn, kg=kg, local_date="2026-11-06")
    assert res.status == "needs_confirmation"


def test_weight_without_history_only_range_check(conn):
    assert logs.log_weight(conn, kg=95.0, local_date="2026-11-06").status == "created"


def test_second_weight_same_day_replaces(conn):
    _seed_weights(conn, WEEK)
    first = logs.log_weight(conn, kg=80.4, local_date="2026-11-06")
    second = logs.log_weight(conn, kg=80.1, local_date="2026-11-06")
    assert first.status == "created"
    assert second.status == "replaced" and second.previous is not None and second.previous["kg"] == 80.4
    assert conn.execute("SELECT kg FROM weight WHERE date = '2026-11-06'").fetchall() == [(80.1,)]
    # the other source's history is untouched
    assert count(conn, "SELECT * FROM weight") == 6


def test_weight_client_ref_idempotent(conn):
    logs.log_weight(conn, kg=80.0, local_date="2026-11-06", client_ref="w1")
    again = logs.log_weight(conn, kg=80.0, local_date="2026-11-06", client_ref="w1")
    assert again.status == "existing"
    assert count(conn, "SELECT * FROM audit") == 1


def test_undo_replaced_weight_restores_previous(conn):
    logs.log_weight(conn, kg=80.4, local_date="2026-11-06")
    second = logs.log_weight(conn, kg=80.1, local_date="2026-11-06")
    assert second.audit_id is not None
    assert plan_ops.undo(conn, second.audit_id).status == "undone"
    assert conn.execute("SELECT kg FROM weight WHERE date = '2026-11-06'").fetchall() == [(80.4,)]


def test_weight_rejects_non_positive(conn):
    with pytest.raises(ValueError):
        logs.log_weight(conn, kg=0)


# --- notes -----------------------------------------------------------------


def test_add_note_with_tags_and_audit(conn):
    res = logs.add_note(conn, text="  left knee sore after climb  ", tags=["knee", " pain "], client_ref="n1")
    assert res.status == "created" and res.row is not None
    assert res.row["text"] == "left knee sore after climb" and res.row["tags"] == "knee,pain"
    assert logs.add_note(conn, text="dup", client_ref="n1").status == "existing"
    assert len(_audits(conn, "note")) == 1


def test_note_rejects_empty_and_comma_tags(conn):
    with pytest.raises(ValueError):
        logs.add_note(conn, text="   ")
    with pytest.raises(ValueError):
        logs.add_note(conn, text="x", tags=["a,b"])


# --- supplements -----------------------------------------------------------


def test_upsert_supplement_create_unchanged_update(conn):
    created = logs.upsert_supplement(conn, name="Magnesium", timing="evening", dose="300 mg")
    same = logs.upsert_supplement(conn, name="Magnesium", timing="evening", dose="300 mg")
    updated = logs.upsert_supplement(conn, name="magnesium", timing="evening", dose="400 mg")  # name match ignores case
    assert (created.status, same.status, updated.status) == ("created", "existing", "updated")
    assert updated.previous is not None and updated.previous["dose"] == "300 mg"
    assert count(conn, "SELECT * FROM supplement") == 1
    assert len(_audits(conn, "supplement")) == 2


def test_upsert_supplement_errors(conn):
    with pytest.raises(ValueError):
        logs.upsert_supplement(conn, name="Iron", timing="midnight")
    assert logs.upsert_supplement(conn, name="Iron", timing="morning", supplement_id=99).status == "not_found"


def test_log_supplement_by_name_and_dedupe(conn):
    logs.upsert_supplement(conn, name="Vitamin D", timing="morning")
    first = logs.log_supplement(conn, supplement="vitamin d", local_date="2026-11-06")
    again = logs.log_supplement(conn, supplement="Vitamin D", local_date="2026-11-06")
    assert first.status == "created" and first.row is not None and first.row["timing"] == "morning"
    assert again.status == "existing"
    assert logs.log_supplement(conn, supplement="Vitamin D", local_date="2026-11-06",
                               timing="evening").status == "created"
    assert count(conn, "SELECT * FROM supplement_intake") == 2


def test_log_supplement_client_ref_and_unknown(conn):
    logs.upsert_supplement(conn, name="Creatine", timing="with_meal")
    logs.log_supplement(conn, supplement="Creatine", client_ref="s1", local_date="2026-11-05")
    assert logs.log_supplement(conn, supplement="Creatine", client_ref="s1").status == "existing"
    assert logs.log_supplement(conn, supplement="Unobtainium").status == "not_found"


# --- weekly review ---------------------------------------------------------


def test_weekly_review_create_existing_replace(conn):
    a = logs.save_weekly_review(conn, week_start="2026-11-02", text="Solid week.")
    b = logs.save_weekly_review(conn, week_start="2026-11-02", text="Solid week.")
    c = logs.save_weekly_review(conn, week_start="2026-11-02", text="Solid week, sleep short Thursday.")
    assert (a.status, b.status, c.status) == ("created", "existing", "replaced")
    assert count(conn, "SELECT * FROM weekly_review") == 1
    assert c.audit_id is not None and plan_ops.undo(conn, c.audit_id).status == "undone"
    assert conn.execute("SELECT text FROM weekly_review").fetchone() == ("Solid week.",)


def test_weekly_review_requires_monday(conn):
    with pytest.raises(ValueError):
        logs.save_weekly_review(conn, week_start="2026-11-04", text="x")


def test_actor_validation():
    assert audit.check_actor("job:auto_ease") == "job:auto_ease"
    assert audit.is_job("job") and audit.is_job("job:x") and not audit.is_job("claude")
    with pytest.raises(ValueError):
        audit.check_actor("job:")
