CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    start_date_local TEXT NOT NULL CHECK(start_date_local GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    type TEXT NOT NULL,
    name TEXT,
    distance REAL,
    moving_time INTEGER,
    elapsed_time INTEGER,
    average_heartrate INTEGER,
    average_speed REAL,
    icu_warmup_time INTEGER,
    icu_cooldown_time INTEGER,
    classification TEXT NOT NULL,
    display_name TEXT NOT NULL,
    work_interval_time INTEGER DEFAULT 0,
    interval_count INTEGER DEFAULT 0,
    interval_avg_duration INTEGER DEFAULT 0,
    work_interval_distance REAL,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(start_date_local);