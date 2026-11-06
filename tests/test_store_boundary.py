"""Only the store, ingest and the migrator write SQL (KTD9)."""

from __future__ import annotations

import re
from pathlib import Path

COACH = Path(__file__).resolve().parent.parent / "coach"
ALLOWED = ("store/", "ingest/", "db/migrate.py")
WRITE_SQL = re.compile(r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|REPLACE\s+INTO)\b",
                       re.IGNORECASE)


def _offenders() -> list[str]:
    out = []
    for path in sorted(COACH.rglob("*.py")):
        rel = path.relative_to(COACH).as_posix()
        if "__pycache__" in rel or rel.startswith(ALLOWED):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if WRITE_SQL.search(line):
                out.append(f"coach/{rel}:{n}: {line.strip()}")
    return out


def test_sql_writes_only_in_store_ingest_migrate():
    assert _offenders() == [], "SQL writes outside coach/store, coach/ingest, coach/db/migrate.py"


def test_pattern_catches_writes():
    for sql in ("INSERT INTO meal", "insert or replace into x", "UPDATE planned_session SET a=1",
                "DELETE FROM note"):
        assert WRITE_SQL.search(sql)
    assert not WRITE_SQL.search("SELECT * FROM meal WHERE updated_at > ?")
