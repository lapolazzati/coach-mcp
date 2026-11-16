-- 006: manual athlete thresholds (FTP, weight) that override what the device recorded.
-- Plain CREATE TABLE: apply through coach.db.migrate, which runs each file inside one
-- transaction together with its version row.

CREATE TABLE athlete_threshold (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('ftp_w', 'weight_kg')),
    value REAL NOT NULL,
    effective_from TEXT NOT NULL,      -- local date (YYYY-MM-DD) the value applies from
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, effective_from)
);
