-- 005: Garmin Personal API as primary activity source (laps, zone time, FIT).
-- Not idempotent (ALTER TABLE ADD COLUMN): apply only through coach.db.migrate,
-- which runs each file inside one transaction together with its version row.

ALTER TABLE activity ADD COLUMN intensity_factor REAL;
ALTER TABLE activity ADD COLUMN tss REAL;
ALTER TABLE activity ADD COLUMN work_kj REAL;
ALTER TABLE activity ADD COLUMN ftp_used REAL;        -- thresholds the device used for this ride
ALTER TABLE activity ADD COLUMN lthr_used REAL;
ALTER TABLE activity ADD COLUMN max_hr_used REAL;
ALTER TABLE activity ADD COLUMN weight_kg_used REAL;
ALTER TABLE activity ADD COLUMN fit_path TEXT;        -- gzipped original FIT, relative to settings.fit_dir

CREATE TABLE activity_lap (
    activity_id INTEGER NOT NULL REFERENCES activity (id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,                     -- 0-based lap number
    start_utc   TEXT NOT NULL,
    start_s     REAL,                                 -- offset from activity start (stream t)
    duration_s  REAL,                                 -- timer time
    distance_m  REAL,
    avg_power   REAL,
    norm_power  REAL,
    max_power   REAL,
    avg_hr      REAL,
    max_hr      REAL,
    avg_cadence REAL,
    ascent_m    REAL,
    descent_m   REAL,
    trigger     TEXT,                                 -- manual | distance | time | position_* | session_end
    PRIMARY KEY (activity_id, idx)
) WITHOUT ROWID;

CREATE TABLE activity_zone_time (
    activity_id INTEGER NOT NULL REFERENCES activity (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('hr', 'power')),
    zone        INTEGER NOT NULL,                     -- 1-based
    seconds     REAL NOT NULL,
    low         REAL,                                 -- zone floor (bpm or W) when known
    high        REAL,
    PRIMARY KEY (activity_id, kind, zone)
) WITHOUT ROWID;
