-- Sport-specific metric tables and swim lengths (SPEC 7.3b). DDL only; the runner owns transactions.
-- All rows are rebuildable; metrics are nullable (absent is never 0).

CREATE TABLE running_metrics (
    activity_id TEXT PRIMARY KEY REFERENCES activities (activity_id),
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    avg_cadence_spm REAL,
    max_cadence_spm REAL,
    avg_stride_length_m REAL,
    avg_ground_contact_time_ms REAL,
    avg_vertical_oscillation_cm REAL,
    avg_vertical_ratio_pct REAL,
    avg_power_w REAL,
    max_power_w REAL
);

CREATE TABLE swimming_metrics (
    activity_id TEXT PRIMARY KEY REFERENCES activities (activity_id),
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    swim_environment TEXT CHECK (swim_environment IS NULL OR swim_environment IN ('pool', 'open_water')),
    pool_length_m REAL,
    active_lengths INTEGER,
    total_strokes INTEGER,
    avg_swolf REAL,
    avg_distance_per_stroke_m REAL,
    avg_stroke_rate_spm REAL,
    active_swim_duration_s REAL,
    recorded_rest_duration_s REAL,
    length_coverage_status TEXT CHECK (length_coverage_status IS NULL OR length_coverage_status IN ('none', 'complete'))
);

CREATE TABLE cycling_metrics (
    activity_id TEXT PRIMARY KEY REFERENCES activities (activity_id),
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    avg_cadence_rpm REAL,
    max_cadence_rpm REAL,
    avg_power_w REAL,
    max_power_w REAL,
    normalized_power_w REAL,
    training_stress_score REAL,
    intensity_factor REAL
);

CREATE TABLE cardio_metrics (
    activity_id TEXT PRIMARY KEY REFERENCES activities (activity_id),
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    steps INTEGER,
    total_sets INTEGER,
    active_sets INTEGER,
    total_reps INTEGER,
    active_set_duration_s REAL,
    rest_set_duration_s REAL
);

CREATE TABLE swim_lengths (
    activity_id TEXT NOT NULL REFERENCES activities (activity_id),
    length_index INTEGER NOT NULL,
    processing_run_id INTEGER NOT NULL REFERENCES processing_runs (id),
    source_payload_id INTEGER REFERENCES raw_payloads (id),
    lap_index INTEGER,
    start_at_utc TEXT,
    length_type TEXT,
    stroke_type TEXT,
    distance_m REAL,
    timer_duration_s REAL,
    elapsed_duration_s REAL,
    stroke_count INTEGER,
    avg_stroke_rate_spm REAL,
    swolf REAL,
    PRIMARY KEY (activity_id, length_index)
);
