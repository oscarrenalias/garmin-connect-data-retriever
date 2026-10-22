-- Archive and operational schema (SPEC 5.2). DDL only; the runner owns transactions.

CREATE TABLE archive_metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK (json_valid(value_json))
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('full', 'incremental', 'scoped', 'activity', 'resume', 'retry_failed')),
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed', 'interrupted')),
    parameters_json TEXT CHECK (parameters_json IS NULL OR json_valid(parameters_json)),
    counters_json TEXT CHECK (counters_json IS NULL OR json_valid(counters_json)),
    coverage_json TEXT CHECK (coverage_json IS NULL OR json_valid(coverage_json))
);

CREATE TABLE raw_payloads (
    id INTEGER PRIMARY KEY,
    resource_key TEXT NOT NULL,
    activity_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('search_page', 'summary', 'workouts', 'original_file', 'imported_file')),
    content_type TEXT,
    encoding TEXT,
    body_text TEXT,
    body_blob BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    first_seen_at_utc TEXT NOT NULL,
    UNIQUE (resource_key, sha256)
);

CREATE TABLE fetch_events (
    id INTEGER PRIMARY KEY,
    sync_run_id INTEGER REFERENCES sync_runs (id),
    resource_key TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    request_params_json TEXT CHECK (request_params_json IS NULL OR json_valid(request_params_json)),
    fetched_at_utc TEXT NOT NULL,
    http_status INTEGER,
    payload_id INTEGER REFERENCES raw_payloads (id),
    etag TEXT,
    last_modified TEXT,
    error_code TEXT
);

CREATE TABLE activity_archive (
    activity_id TEXT PRIMARY KEY,
    owner_profile_id TEXT,
    parent_activity_id TEXT,
    type_key TEXT,
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc TEXT NOT NULL,
    last_summary_check_at_utc TEXT,
    availability_status TEXT NOT NULL DEFAULT 'listed' CHECK (availability_status IN ('listed', 'not_found', 'forbidden'))
);

CREATE TABLE activity_resources (
    activity_id TEXT NOT NULL REFERENCES activity_archive (activity_id),
    kind TEXT NOT NULL CHECK (kind IN ('search_item', 'summary', 'workouts', 'original_file', 'imported_file')),
    current_payload_id INTEGER REFERENCES raw_payloads (id),
    json_pointer TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'available', 'unavailable', 'forbidden', 'failed')),
    last_attempt_at_utc TEXT,
    last_success_at_utc TEXT,
    next_retry_at_utc TEXT,
    error_code TEXT,
    PRIMARY KEY (activity_id, kind)
);

CREATE TABLE sync_checkpoints (
    scope TEXT PRIMARY KEY,
    window_start TEXT,
    window_end TEXT,
    next_offset INTEGER,
    last_completed_at_utc TEXT,
    run_id INTEGER REFERENCES sync_runs (id)
);

CREATE TABLE processing_runs (
    id INTEGER PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activity_archive (activity_id),
    processor_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'partial', 'unsupported', 'failed')),
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    warnings_json TEXT CHECK (warnings_json IS NULL OR json_valid(warnings_json)),
    error_code TEXT
);

CREATE INDEX idx_raw_payloads_activity_kind ON raw_payloads (activity_id, kind);
CREATE INDEX idx_fetch_events_resource_fetched ON fetch_events (resource_key, fetched_at_utc);
CREATE INDEX idx_fetch_events_sync_run ON fetch_events (sync_run_id);
CREATE INDEX idx_activity_archive_parent ON activity_archive (parent_activity_id);
CREATE INDEX idx_activity_archive_type_key ON activity_archive (type_key);
CREATE INDEX idx_activity_resources_state ON activity_resources (state);
CREATE INDEX idx_processing_runs_activity_started ON processing_runs (activity_id, started_at_utc);
