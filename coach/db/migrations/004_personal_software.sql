-- 004: personal software pivot (Claude app over MCP is the only chat channel).
-- Timestamps are UTC ISO-8601; dates are Europe/Rome local (as in 003).
-- The runner applies each file exactly once (schema_migrations), so the plain
-- ALTER TABLE ADD COLUMN statements below are safe. Unlike 001-003 this file is
-- NOT idempotent: never executescript() it outside coach.db.migrate.
-- planned_session.missing_polls already exists (003) and is reused as-is.

CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT,                                   -- comma-separated
    source TEXT NOT NULL DEFAULT 'claude',
    client_ref TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS note_day ON note(local_date);

CREATE TABLE IF NOT EXISTS supplement (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    dose TEXT,
    timing TEXT NOT NULL CHECK (timing IN ('morning', 'lunch', 'evening', 'with_meal')),
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS supplement_intake (
    id INTEGER PRIMARY KEY,
    supplement_id INTEGER NOT NULL REFERENCES supplement(id),
    local_date TEXT NOT NULL,
    timing TEXT,
    ts_utc TEXT NOT NULL,
    client_ref TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS supplement_intake_day ON supplement_intake(local_date);

CREATE TABLE IF NOT EXISTS readiness_day (
    date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('green', 'amber', 'red', 'unknown')),
    flags_json TEXT NOT NULL DEFAULT '[]',
    signals_json TEXT NOT NULL DEFAULT '{}',
    computed_at TEXT NOT NULL,
    final INTEGER NOT NULL DEFAULT 0,
    sleep_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS session_tombstone (
    external_id TEXT NOT NULL,
    session_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    pending_delete INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (external_id, session_id)
);

CREATE TABLE IF NOT EXISTS month_plan (
    month TEXT PRIMARY KEY,                      -- YYYY-MM
    rationale TEXT,
    plan_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_summary (
    week_start TEXT PRIMARY KEY,                 -- Monday, YYYY-MM-DD
    metrics_json TEXT NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_review (
    week_start TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'claude',
    saved_at TEXT NOT NULL
);

ALTER TABLE planned_session ADD COLUMN ease_reason TEXT;
ALTER TABLE planned_session ADD COLUMN eased_from_json TEXT;
ALTER TABLE planned_session ADD COLUMN eased_at TEXT;
ALTER TABLE planned_session ADD COLUMN sync_dirty INTEGER NOT NULL DEFAULT 0;
ALTER TABLE planned_session ADD COLUMN pinned_date INTEGER NOT NULL DEFAULT 0;
ALTER TABLE planned_session ADD COLUMN row_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE planned_session ADD COLUMN start_time TEXT;

ALTER TABLE meal ADD COLUMN client_ref TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS meal_client_ref ON meal(client_ref) WHERE client_ref IS NOT NULL;
ALTER TABLE weight ADD COLUMN client_ref TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS weight_client_ref ON weight(client_ref) WHERE client_ref IS NOT NULL;
-- One Claude-logged weight per day: a second scale photo upserts this row.
CREATE UNIQUE INDEX IF NOT EXISTS weight_claude_day ON weight(date) WHERE source = 'claude';

-- Telegram / in-app LLM leftovers (KTD13).
DROP TABLE IF EXISTS message_log;
DROP TABLE IF EXISTS pending_choice;
DROP TABLE IF EXISTS llm_call;
