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

-- Stable analytical views (SPEC 7.4). Sport views exclude multisport parents; paces are never averaged.

-- v_activity_summary: one row per activity (all, incl. multisport parents); metres/seconds/bpm; timer_duration_s excludes pauses, elapsed_duration_s includes them. Apply include_in_volume_totals when summing.
CREATE VIEW v_activity_summary AS
SELECT
    a.*,
    CASE WHEN a.is_multisport_parent = 1 THEN 0 ELSE 1 END AS include_in_volume_totals
FROM activities AS a;

-- v_running: one row per running activity (no multisport parents); metres/seconds/bpm; timer_pace_s_per_km uses timer time (excludes pauses), NULL unless distance_m > 0.
CREATE VIEW v_running AS
SELECT
    a.*,
    m.avg_cadence_spm,
    m.max_cadence_spm,
    m.avg_stride_length_m,
    m.avg_ground_contact_time_ms,
    m.avg_vertical_oscillation_cm,
    m.avg_vertical_ratio_pct,
    m.avg_power_w,
    m.max_power_w,
    CASE WHEN a.distance_m > 0 THEN a.timer_duration_s * 1000.0 / a.distance_m END AS timer_pace_s_per_km
FROM activities AS a
JOIN running_metrics AS m ON m.activity_id = a.activity_id
WHERE a.sport_family = 'running' AND COALESCE(a.is_multisport_parent, 0) = 0;

-- v_swimming: one row per swimming activity (no multisport parents); metres/seconds; timer pace includes rests, active pace uses active_swim_duration_s only (rests excluded) and is NULL when unknown or distance_m <= 0.
CREATE VIEW v_swimming AS
SELECT
    a.*,
    m.swim_environment,
    m.pool_length_m,
    m.active_lengths,
    m.total_strokes,
    m.avg_swolf,
    m.avg_distance_per_stroke_m,
    m.avg_stroke_rate_spm,
    m.active_swim_duration_s,
    m.recorded_rest_duration_s,
    m.length_coverage_status,
    CASE WHEN a.distance_m > 0 THEN a.timer_duration_s * 100.0 / a.distance_m END AS timer_pace_s_per_100m,
    CASE WHEN a.distance_m > 0 AND m.active_swim_duration_s IS NOT NULL
        THEN m.active_swim_duration_s * 100.0 / a.distance_m END AS active_pace_s_per_100m
FROM activities AS a
JOIN swimming_metrics AS m ON m.activity_id = a.activity_id
WHERE a.sport_family = 'swimming' AND COALESCE(a.is_multisport_parent, 0) = 0;

-- v_swim_lengths: one row per pool length (active and idle; no multisport parents); metres/seconds; timer_duration_s is per-length timer time, pace_s_per_100m only for active lengths with distance_m > 0 (rest lengths excluded).
CREATE VIEW v_swim_lengths AS
SELECT
    l.*,
    a.local_date,
    CASE WHEN l.length_type = 'active' AND l.distance_m > 0
        THEN l.timer_duration_s * 100.0 / l.distance_m END AS pace_s_per_100m
FROM swim_lengths AS l
JOIN activities AS a ON a.activity_id = l.activity_id
WHERE COALESCE(a.is_multisport_parent, 0) = 0;

-- v_cycling: one row per cycling activity (no multisport parents); metres/seconds/watts/rpm; timer_duration_s excludes pauses, elapsed_duration_s includes them.
CREATE VIEW v_cycling AS
SELECT
    a.*,
    m.avg_cadence_rpm,
    m.max_cadence_rpm,
    m.avg_power_w,
    m.max_power_w,
    m.normalized_power_w,
    m.training_stress_score,
    m.intensity_factor
FROM activities AS a
JOIN cycling_metrics AS m ON m.activity_id = a.activity_id
WHERE a.sport_family = 'cycling' AND COALESCE(a.is_multisport_parent, 0) = 0;

-- v_cardio: one row per cardio activity (no multisport parents); seconds/counts; timer_duration_s excludes pauses, set durations split active sets from recorded rest sets.
CREATE VIEW v_cardio AS
SELECT
    a.*,
    m.steps,
    m.total_sets,
    m.active_sets,
    m.total_reps,
    m.active_set_duration_s,
    m.rest_set_duration_s
FROM activities AS a
JOIN cardio_metrics AS m ON m.activity_id = a.activity_id
WHERE a.sport_family = 'cardio' AND COALESCE(a.is_multisport_parent, 0) = 0;
