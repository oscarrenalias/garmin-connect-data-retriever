-- FORM goggles sessions and lengths. DDL only; the runner owns transactions.
-- Rows are rebuilt wholesale from imported_file:form:* payloads; there are no processing_runs rows.
-- No device serial numbers are stored. Metrics are nullable (absent is never 0).

CREATE TABLE form_sessions (
    form_session_id TEXT PRIMARY KEY,
    source_payload_id INTEGER NOT NULL REFERENCES raw_payloads (id),
    session_index INTEGER,
    start_at_utc TEXT,
    local_date TEXT,
    environment TEXT CHECK (environment IS NULL OR environment IN ('pool', 'open_water')),
    pool_length_m REAL,
    distance_m REAL,
    active_lengths INTEGER,
    active_swim_duration_s REAL,
    rest_duration_s REAL,
    timer_duration_s REAL,
    elapsed_duration_s REAL,
    total_strokes INTEGER,
    avg_hr_bpm REAL,
    max_hr_bpm REAL,
    matched_activity_id TEXT,
    match_delta_s REAL,
    pool_length_mismatch INTEGER CHECK (pool_length_mismatch IS NULL OR pool_length_mismatch IN (0, 1))
);

CREATE TABLE form_lengths (
    form_session_id TEXT NOT NULL REFERENCES form_sessions (form_session_id),
    length_index INTEGER NOT NULL,
    source_payload_id INTEGER NOT NULL REFERENCES raw_payloads (id),
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
    likely_unrecorded_swim INTEGER CHECK (likely_unrecorded_swim IS NULL OR likely_unrecorded_swim IN (0, 1)),
    PRIMARY KEY (form_session_id, length_index)
);
