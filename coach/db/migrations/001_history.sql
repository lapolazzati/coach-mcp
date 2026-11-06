-- History tables from the U1 table list (activity, wellness_day, weight), plus
-- two tables U12 needs for the power curve and segment comparisons
-- (activity_stream, activity_power_best). Written for the Garmin export
-- importer; U5's intervals.icu ingestion fills the same rows.
-- Every statement is idempotent.

CREATE TABLE IF NOT EXISTS activity (
    id                 INTEGER PRIMARY KEY,
    garmin_activity_id INTEGER UNIQUE,
    icu_activity_id    TEXT UNIQUE,
    source             TEXT NOT NULL,           -- 'garmin_export' | 'intervals'
    sport              TEXT NOT NULL,           -- raw type, e.g. road_biking
    sport_group        TEXT NOT NULL,           -- cycling | running | strength | swimming | other
    indoor             INTEGER NOT NULL DEFAULT 0,
    name               TEXT,
    start_utc          TEXT NOT NULL,           -- ISO 8601, UTC
    start_local        TEXT,                    -- ISO 8601, athlete's local wall time
    duration_s         REAL,
    moving_s           REAL,
    elapsed_s          REAL,
    distance_m         REAL,
    elevation_gain_m   REAL,
    avg_speed_mps      REAL,
    avg_hr             REAL,
    max_hr             REAL,
    avg_power          REAL,
    max_power          REAL,
    norm_power         REAL,
    max_20min_power    REAL,
    avg_cadence        REAL,
    calories           REAL,
    training_load      REAL,
    aerobic_te         REAL,
    anaerobic_te       REAL,
    raw_json           TEXT,                    -- full source record, for fields not mapped yet
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_start ON activity (start_utc);
CREATE INDEX IF NOT EXISTS activity_group_start ON activity (sport_group, start_utc);

-- One row per calendar day (athlete's local date). Each source fills only its
-- own columns; a re-import never blanks a column another source wrote.
CREATE TABLE IF NOT EXISTS wellness_day (
    date              TEXT PRIMARY KEY,         -- YYYY-MM-DD
    sleep_secs        INTEGER,
    sleep_score       INTEGER,
    sleep_deep_secs   INTEGER,
    sleep_light_secs  INTEGER,
    sleep_rem_secs    INTEGER,
    sleep_awake_secs  INTEGER,
    sleep_start_utc   TEXT,
    sleep_end_utc     TEXT,
    sleep_avg_stress  REAL,
    hrv_ms            REAL,                     -- overnight HRV
    resting_hr        INTEGER,
    avg_stress        INTEGER,
    body_battery_high INTEGER,
    body_battery_low  INTEGER,
    steps             INTEGER,
    readiness_score   INTEGER,                  -- Garmin Training Readiness, morning value
    readiness_level   TEXT,
    hrv_weekly_avg    REAL,
    acute_load        REAL,
    chronic_load      REAL,
    acwr              REAL,
    ctl               REAL,                     -- intervals.icu (U5)
    atl               REAL,                     -- intervals.icu (U5)
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weight (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,                  -- YYYY-MM-DD
    kg          REAL NOT NULL,
    source      TEXT NOT NULL,                  -- 'garmin_export' | 'intervals' | 'telegram'
    updated_at  TEXT NOT NULL,
    UNIQUE (date, source)
);

-- 1 Hz samples, t = seconds since activity start. Kept only for recent rides.
CREATE TABLE IF NOT EXISTS activity_stream (
    activity_id INTEGER NOT NULL REFERENCES activity (id) ON DELETE CASCADE,
    t           INTEGER NOT NULL,
    power       INTEGER,
    hr          INTEGER,
    cadence     INTEGER,
    speed_mps   REAL,
    distance_m  REAL,
    altitude_m  REAL,
    lat         REAL,
    lon         REAL,
    PRIMARY KEY (activity_id, t)
) WITHOUT ROWID;

-- Mean-maximal power per activity; the power curve is MAX(watts) over a date range.
CREATE TABLE IF NOT EXISTS activity_power_best (
    activity_id INTEGER NOT NULL REFERENCES activity (id) ON DELETE CASCADE,
    duration_s  INTEGER NOT NULL,
    watts       REAL NOT NULL,
    PRIMARY KEY (activity_id, duration_s)
) WITHOUT ROWID;
