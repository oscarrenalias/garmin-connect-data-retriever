-- Derived activity tables (SPEC 7.2, 7.3a). DDL only; the runner owns transactions.
-- All rows are rebuildable; metrics are nullable (absent is never 0).

CREATE TABLE activities (
    activity_id TEXT PRIMARY KEY REFERENCES activity_archive (activity_id),
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    parent_activity_id TEXT,
    is_multisport_parent INTEGER CHECK (is_multisport_parent IS NULL OR is_multisport_parent IN (0, 1)),
    type_key TEXT,
    sport_family TEXT,
    name TEXT,
    description TEXT,
    start_at_utc TEXT,
    start_local TEXT,
    local_date TEXT,
    timezone TEXT,
    distance_m REAL,
    timer_duration_s REAL,
    elapsed_duration_s REAL,
    moving_duration_s REAL,
    avg_speed_mps REAL,
    max_speed_mps REAL,
    avg_hr_bpm REAL,
    max_hr_bpm REAL,
    calories_kcal REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    aerobic_training_effect REAL,
    anaerobic_training_effect REAL,
    training_load REAL,
    processing_status TEXT NOT NULL,
    processed_at_utc TEXT
);

CREATE TABLE activity_sessions (
    activity_id TEXT NOT NULL REFERENCES activities (activity_id),
    session_index INTEGER NOT NULL,
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    fit_sport TEXT,
    fit_subsport TEXT,
    start_at_utc TEXT,
    distance_m REAL,
    timer_duration_s REAL,
    elapsed_duration_s REAL,
    avg_hr_bpm REAL,
    max_hr_bpm REAL,
    fit_message_index INTEGER,
    PRIMARY KEY (activity_id, session_index)
);

CREATE TABLE activity_laps (
    activity_id TEXT NOT NULL REFERENCES activities (activity_id),
    lap_index INTEGER NOT NULL,
    session_index INTEGER NOT NULL,
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    start_at_utc TEXT,
    distance_m REAL,
    timer_duration_s REAL,
    elapsed_duration_s REAL,
    avg_hr_bpm REAL,
    max_hr_bpm REAL,
    avg_speed_mps REAL,
    cadence REAL,
    cadence_unit TEXT,
    lap_trigger TEXT,
    intensity TEXT,
    PRIMARY KEY (activity_id, lap_index),
    FOREIGN KEY (activity_id, session_index) REFERENCES activity_sessions (activity_id, session_index)
);

CREATE TABLE activity_samples (
    activity_id TEXT NOT NULL REFERENCES activities (activity_id),
    sample_index INTEGER NOT NULL,
    session_index INTEGER,
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    timestamp_utc TEXT,
    elapsed_s REAL,
    distance_m REAL,
    speed_mps REAL,
    hr_bpm REAL,
    cadence REAL,
    cadence_unit TEXT,
    altitude_m REAL,
    latitude_deg REAL,
    longitude_deg REAL,
    power_w REAL,
    temperature_c REAL,
    PRIMARY KEY (activity_id, sample_index)
);

CREATE TABLE activity_sets (
    activity_id TEXT NOT NULL REFERENCES activities (activity_id),
    set_index INTEGER NOT NULL,
    session_index INTEGER,
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    set_type TEXT,
    start_at_utc TEXT,
    duration_s REAL,
    repetitions INTEGER,
    weight_kg REAL,
    category TEXT,
    PRIMARY KEY (activity_id, set_index)
);

CREATE INDEX idx_activities_local_date_family ON activities (local_date, sport_family);
CREATE INDEX idx_activities_parent ON activities (parent_activity_id);
CREATE INDEX idx_activities_processing_status ON activities (processing_status);
CREATE INDEX idx_activity_samples_timestamp ON activity_samples (activity_id, timestamp_utc);
