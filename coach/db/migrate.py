"""Apply numbered SQL migrations (NNN_name.sql) once each, in order.

    python -m coach.db.migrate [db_path]

Applied versions are recorded in schema_migrations. Migrations are written to
be idempotent too, so a DB created by the old ensure_schema() stand-in picks
up its history here without errors.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(db_path: Path | str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrations() -> list[tuple[int, Path]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        out.append((int(path.name.split("_", 1)[0]), path))
    versions = [v for v, _ in out]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration numbers in {MIGRATIONS_DIR}")
    return out


def applied(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; return the names applied this run."""
    done = applied(conn)
    ran = []
    for version, path in migrations():
        if version in done:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
            (version, path.name, datetime.now(UTC).isoformat(timespec="seconds")),
        )
        conn.commit()
        ran.append(path.name)
    return ran


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def main(argv: list[str]) -> None:
    from coach.config import get_settings

    db_path = Path(argv[0]) if argv else get_settings().coach_db_path
    conn = connect(db_path)
    try:
        ran = migrate(conn)
        print(f"{db_path}: applied {ran or 'nothing'}; at version {current_version(conn)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
