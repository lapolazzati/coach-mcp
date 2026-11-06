-- Garmin VO2max estimates. Sparse (a new value only after a qualifying run or
-- ride), so it gets its own table instead of wellness_day columns.

CREATE TABLE IF NOT EXISTS vo2max (
    date               TEXT NOT NULL,           -- YYYY-MM-DD
    sport              TEXT NOT NULL,           -- 'running' | 'cycling'
    vo2max             REAL NOT NULL,           -- ml/kg/min as Garmin displays it (whole number)
    vo2max_precise     REAL,                    -- maxMet * 3.5, unrounded; NULL for older records
    garmin_activity_id INTEGER,                 -- activity that produced it, when Garmin says
    source             TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (date, sport)
);
