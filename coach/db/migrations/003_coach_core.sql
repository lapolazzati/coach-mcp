-- 003: coach core. Timestamps are UTC ISO-8601 (KTD8); dates are Europe/Rome local.

CREATE TABLE IF NOT EXISTS athlete (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    ftp_w INTEGER,
    weight_kg REAL,
    target_weight_kg REAL,
    wake_earliest TEXT NOT NULL DEFAULT '06:45',
    home_address TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    distance_km REAL,
    elevation_m REAL,
    target_a TEXT, target_b TEXT, target_c TEXT,
    gpx_path TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS season_plan (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goal(id),
    version INTEGER NOT NULL,
    phases_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (goal_id, version)
);

CREATE TABLE IF NOT EXISTS planned_session (
    id INTEGER PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,            -- coach:{date}:{slot}
    date TEXT NOT NULL,
    slot TEXT NOT NULL,
    start_local TEXT,
    duration_min INTEGER NOT NULL,
    sport TEXT NOT NULL DEFAULT 'Ride',
    kind TEXT NOT NULL,                          -- endurance, threshold, vo2, rest, ...
    name TEXT NOT NULL,
    session_json TEXT NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 1,
    icu_event_id INTEGER,
    last_written_hash TEXT,
    gcal_event_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned',      -- planned, done, missed, skipped, moved
    user_locked INTEGER NOT NULL DEFAULT 0,
    deleted_by_coach INTEGER NOT NULL DEFAULT 0,
    missing_polls INTEGER NOT NULL DEFAULT 0,
    matched_activity_id INTEGER REFERENCES activity(id),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS planned_session_date ON planned_session(date);

CREATE TABLE IF NOT EXISTS calendar_event (
    id INTEGER PRIMARY KEY,
    calendar_id TEXT NOT NULL,
    gcal_id TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    location TEXT,
    attendee_count INTEGER NOT NULL DEFAULT 0,
    kind TEXT,                                   -- meeting, travel, personal, coach
    busy INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'confirmed',
    updated_at TEXT NOT NULL,
    UNIQUE (calendar_id, gcal_id)
);
CREATE INDEX IF NOT EXISTS calendar_event_start ON calendar_event(start_utc);

CREATE TABLE IF NOT EXISTS trip (
    id INTEGER PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    destination TEXT,
    source TEXT NOT NULL,                        -- calendar, user
    bike_available INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    direction TEXT NOT NULL,                     -- out, in
    category TEXT,                               -- safety, choice, evening, morning, food, progress, health, reply
    proactive INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    telegram_message_id INTEGER,
    dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS message_log_day ON message_log(local_date, proactive);

CREATE TABLE IF NOT EXISTS pending_choice (
    id INTEGER PRIMARY KEY,
    created_utc TEXT NOT NULL,
    expires_utc TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    default_option TEXT NOT NULL,
    context_json TEXT,
    answer TEXT,
    resolved_utc TEXT,
    resolution TEXT                              -- answered, expired_default
);

CREATE TABLE IF NOT EXISTS meal (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    description TEXT,
    kcal INTEGER,
    carbs_g REAL, protein_g REAL, fat_g REAL,
    source TEXT NOT NULL,                        -- photo, text
    confidence REAL,
    confirmed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    actor TEXT NOT NULL,                         -- coach, user, sync
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    before_json TEXT,
    after_json TEXT,
    reason TEXT,
    undone INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_call (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    task TEXT NOT NULL,
    tier_requested TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,                        -- ok, parse_error, refusal, api_error
    error TEXT
);
CREATE INDEX IF NOT EXISTS llm_call_ts ON llm_call(ts_utc);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    lat REAL, lng REAL,
    formatted TEXT,
    fetched_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_cache (
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    mode TEXT NOT NULL,
    depart_bucket TEXT NOT NULL,                 -- weekday+hour bucket
    minutes INTEGER NOT NULL,
    fetched_utc TEXT NOT NULL,
    PRIMARY KEY (origin, destination, mode, depart_bucket)
);
