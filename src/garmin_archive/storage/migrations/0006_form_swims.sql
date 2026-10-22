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

-- v_swimming: FORM precedence. Matched pool swims take swim totals from FORM; HR, calories, training metrics and all other
-- columns stay Garmin; garmin_* hold the watch values. Open-water matches stay Garmin. Unmatched FORM sessions that overlap
-- no Garmin swim (any environment) are added as FORM-only rows; unmatched sessions that overlap a Garmin swim are excluded.
-- Columns are listed explicitly (no a.*): existing columns keep name and order, new columns are appended.
DROP VIEW v_swimming;

CREATE VIEW v_swimming AS
WITH form_agg AS (
    SELECT
        form_session_id,
        SUM(distance_m) AS active_distance_m,
        SUM(stroke_count) AS active_strokes,
        AVG(swolf) AS avg_swolf,
        AVG(avg_stroke_rate_spm) AS avg_stroke_rate_spm
    FROM form_lengths
    WHERE length_type = 'active'
    GROUP BY form_session_id
)
SELECT
    a.activity_id,
    a.processing_run_id,
    a.source_payload_id,
    a.parent_activity_id,
    a.is_multisport_parent,
    a.type_key,
    a.sport_family,
    a.name,
    a.description,
    a.start_at_utc,
    a.start_local,
    a.local_date,
    a.timezone,
    CASE WHEN f.form_session_id IS NOT NULL THEN f.distance_m ELSE a.distance_m END AS distance_m,
    a.timer_duration_s,
    a.elapsed_duration_s,
    a.moving_duration_s,
    a.avg_speed_mps,
    a.max_speed_mps,
    a.avg_hr_bpm,
    a.max_hr_bpm,
    a.calories_kcal,
    a.elevation_gain_m,
    a.elevation_loss_m,
    a.aerobic_training_effect,
    a.anaerobic_training_effect,
    a.training_load,
    a.processing_status,
    a.processed_at_utc,
    m.swim_environment,
    m.pool_length_m,
    CASE WHEN f.form_session_id IS NOT NULL THEN f.active_lengths ELSE m.active_lengths END AS active_lengths,
    CASE WHEN f.form_session_id IS NOT NULL THEN f.total_strokes ELSE m.total_strokes END AS total_strokes,
    CASE WHEN f.form_session_id IS NOT NULL THEN g.avg_swolf ELSE m.avg_swolf END AS avg_swolf,
    CASE WHEN f.form_session_id IS NOT NULL
        THEN CASE WHEN g.active_strokes > 0 THEN g.active_distance_m / g.active_strokes END
        ELSE m.avg_distance_per_stroke_m END AS avg_distance_per_stroke_m,
    CASE WHEN f.form_session_id IS NOT NULL THEN g.avg_stroke_rate_spm ELSE m.avg_stroke_rate_spm END AS avg_stroke_rate_spm,
    CASE WHEN f.form_session_id IS NOT NULL THEN f.active_swim_duration_s ELSE m.active_swim_duration_s END AS active_swim_duration_s,
    CASE WHEN f.form_session_id IS NOT NULL THEN f.rest_duration_s ELSE m.recorded_rest_duration_s END AS recorded_rest_duration_s,
    CASE WHEN f.form_session_id IS NOT NULL THEN f.rest_duration_s ELSE m.probable_rest_duration_s END AS probable_rest_duration_s,
    CASE WHEN f.form_session_id IS NOT NULL THEN NULL ELSE m.likely_unrecorded_swim_duration_s END AS likely_unrecorded_swim_duration_s,
    CASE WHEN f.form_session_id IS NOT NULL THEN 'complete' ELSE m.length_coverage_status END AS length_coverage_status,
    CASE
        WHEN f.form_session_id IS NOT NULL THEN
            CASE WHEN f.distance_m > 0 THEN f.timer_duration_s * 100.0 / f.distance_m END
        WHEN a.distance_m > 0 THEN a.timer_duration_s * 100.0 / a.distance_m
    END AS timer_pace_s_per_100m,
    CASE
        WHEN f.form_session_id IS NOT NULL THEN
            CASE WHEN f.distance_m > 0 AND f.active_swim_duration_s IS NOT NULL
                THEN f.active_swim_duration_s * 100.0 / f.distance_m END
        WHEN a.distance_m > 0 AND m.active_swim_duration_s IS NOT NULL
            THEN m.active_swim_duration_s * 100.0 / a.distance_m
    END AS active_pace_s_per_100m,
    CASE WHEN f.form_session_id IS NOT NULL THEN 'form' ELSE 'garmin' END AS data_source,
    a.distance_m AS garmin_distance_m,
    m.active_lengths AS garmin_active_lengths,
    m.active_swim_duration_s AS garmin_active_swim_duration_s,
    m.recorded_rest_duration_s AS garmin_recorded_rest_duration_s
FROM activities AS a
JOIN swimming_metrics AS m ON m.activity_id = a.activity_id
LEFT JOIN form_sessions AS f
    ON f.matched_activity_id = a.activity_id AND f.environment = 'pool'
LEFT JOIN form_agg AS g ON g.form_session_id = f.form_session_id
WHERE a.sport_family = 'swimming' AND COALESCE(a.is_multisport_parent, 0) = 0

UNION ALL

SELECT
    s.form_session_id,
    NULL, NULL, NULL, NULL,
    CASE s.environment WHEN 'pool' THEN 'lap_swimming' WHEN 'open_water' THEN 'open_water_swimming' END,
    'swimming',
    NULL, NULL,
    s.start_at_utc,
    NULL,
    s.local_date,
    NULL,
    s.distance_m,
    s.timer_duration_s,
    s.elapsed_duration_s,
    NULL, NULL, NULL,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    'processed',
    NULL,
    s.environment,
    s.pool_length_m,
    s.active_lengths,
    s.total_strokes,
    g.avg_swolf,
    CASE WHEN g.active_strokes > 0 THEN g.active_distance_m / g.active_strokes END,
    g.avg_stroke_rate_spm,
    s.active_swim_duration_s,
    s.rest_duration_s,
    s.rest_duration_s,
    NULL,
    'complete',
    CASE WHEN s.distance_m > 0 THEN s.timer_duration_s * 100.0 / s.distance_m END,
    CASE WHEN s.distance_m > 0 AND s.active_swim_duration_s IS NOT NULL
        THEN s.active_swim_duration_s * 100.0 / s.distance_m END,
    'form',
    NULL, NULL, NULL, NULL
FROM form_sessions AS s
LEFT JOIN form_agg AS g ON g.form_session_id = s.form_session_id
WHERE s.matched_activity_id IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM activities AS ga
    WHERE ga.sport_family = 'swimming'
      AND COALESCE(ga.is_multisport_parent, 0) = 0
      AND julianday(s.start_at_utc) < julianday(ga.start_at_utc)
          + COALESCE(ga.elapsed_duration_s, ga.timer_duration_s, 0) / 86400.0
      AND julianday(ga.start_at_utc) < julianday(s.start_at_utc)
          + COALESCE(s.elapsed_duration_s, s.timer_duration_s, 0) / 86400.0
  );

-- v_swim_lengths: matched pool swims and FORM-only sessions take lengths from form_lengths (data_source='form'; includes drill;
-- likely_unrecorded_swim NULL; activity_id is the Garmin id when matched, else form_session_id). Everything else keeps the Garmin lengths.
-- Columns are listed explicitly: existing columns keep name and order, data_source is appended.
DROP VIEW v_swim_lengths;

CREATE VIEW v_swim_lengths AS
SELECT
    l.activity_id,
    l.length_index,
    l.processing_run_id,
    l.source_payload_id,
    l.lap_index,
    l.start_at_utc,
    l.length_type,
    l.stroke_type,
    l.distance_m,
    l.timer_duration_s,
    l.elapsed_duration_s,
    l.stroke_count,
    l.avg_stroke_rate_spm,
    l.swolf,
    l.likely_unrecorded_swim,
    a.local_date,
    CASE WHEN l.length_type = 'active' AND l.distance_m > 0
        THEN l.timer_duration_s * 100.0 / l.distance_m END AS pace_s_per_100m,
    'garmin' AS data_source
FROM swim_lengths AS l
JOIN activities AS a ON a.activity_id = l.activity_id
WHERE COALESCE(a.is_multisport_parent, 0) = 0
  AND NOT EXISTS (
    SELECT 1 FROM form_sessions AS f
    WHERE f.matched_activity_id = a.activity_id AND f.environment = 'pool'
  )

UNION ALL

SELECT
    a.activity_id,
    fl.length_index,
    NULL,
    fl.source_payload_id,
    fl.lap_index,
    fl.start_at_utc,
    fl.length_type,
    fl.stroke_type,
    fl.distance_m,
    fl.timer_duration_s,
    fl.elapsed_duration_s,
    fl.stroke_count,
    fl.avg_stroke_rate_spm,
    fl.swolf,
    NULL,
    a.local_date,
    CASE WHEN fl.length_type = 'active' AND fl.distance_m > 0
        THEN fl.timer_duration_s * 100.0 / fl.distance_m END,
    'form'
FROM form_sessions AS f
JOIN form_lengths AS fl ON fl.form_session_id = f.form_session_id
JOIN activities AS a ON a.activity_id = f.matched_activity_id
WHERE f.environment = 'pool' AND COALESCE(a.is_multisport_parent, 0) = 0

UNION ALL

SELECT
    s.form_session_id,
    fl.length_index,
    NULL,
    fl.source_payload_id,
    fl.lap_index,
    fl.start_at_utc,
    fl.length_type,
    fl.stroke_type,
    fl.distance_m,
    fl.timer_duration_s,
    fl.elapsed_duration_s,
    fl.stroke_count,
    fl.avg_stroke_rate_spm,
    fl.swolf,
    NULL,
    s.local_date,
    CASE WHEN fl.length_type = 'active' AND fl.distance_m > 0
        THEN fl.timer_duration_s * 100.0 / fl.distance_m END,
    'form'
FROM form_sessions AS s
JOIN form_lengths AS fl ON fl.form_session_id = s.form_session_id
WHERE s.matched_activity_id IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM activities AS ga
    WHERE ga.sport_family = 'swimming'
      AND COALESCE(ga.is_multisport_parent, 0) = 0
      AND julianday(s.start_at_utc) < julianday(ga.start_at_utc)
          + COALESCE(ga.elapsed_duration_s, ga.timer_duration_s, 0) / 86400.0
      AND julianday(ga.start_at_utc) < julianday(s.start_at_utc)
          + COALESCE(s.elapsed_duration_s, s.timer_duration_s, 0) / 86400.0
  );

-- v_activity_summary: Garmin activities stay pure Garmin (matched swims are not overridden); FORM-only sessions (same
-- definition as v_swimming) are added as swimming rows with data_source='form'. Apply include_in_volume_totals when summing.
DROP VIEW v_activity_summary;

CREATE VIEW v_activity_summary AS
SELECT
    a.activity_id,
    a.processing_run_id,
    a.source_payload_id,
    a.parent_activity_id,
    a.is_multisport_parent,
    a.type_key,
    a.sport_family,
    a.name,
    a.description,
    a.start_at_utc,
    a.start_local,
    a.local_date,
    a.timezone,
    a.distance_m,
    a.timer_duration_s,
    a.elapsed_duration_s,
    a.moving_duration_s,
    a.avg_speed_mps,
    a.max_speed_mps,
    a.avg_hr_bpm,
    a.max_hr_bpm,
    a.calories_kcal,
    a.elevation_gain_m,
    a.elevation_loss_m,
    a.aerobic_training_effect,
    a.anaerobic_training_effect,
    a.training_load,
    a.processing_status,
    a.processed_at_utc,
    CASE WHEN a.is_multisport_parent = 1 THEN 0 ELSE 1 END AS include_in_volume_totals,
    'garmin' AS data_source
FROM activities AS a

UNION ALL

SELECT
    s.form_session_id,
    NULL, NULL, NULL, NULL,
    CASE s.environment WHEN 'pool' THEN 'lap_swimming' WHEN 'open_water' THEN 'open_water_swimming' END,
    'swimming',
    NULL, NULL,
    s.start_at_utc,
    NULL,
    s.local_date,
    NULL,
    s.distance_m,
    s.timer_duration_s,
    s.elapsed_duration_s,
    NULL, NULL, NULL,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    'processed',
    NULL,
    1,
    'form'
FROM form_sessions AS s
WHERE s.matched_activity_id IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM activities AS ga
    WHERE ga.sport_family = 'swimming'
      AND COALESCE(ga.is_multisport_parent, 0) = 0
      AND julianday(s.start_at_utc) < julianday(ga.start_at_utc)
          + COALESCE(ga.elapsed_duration_s, ga.timer_duration_s, 0) / 86400.0
      AND julianday(ga.start_at_utc) < julianday(s.start_at_utc)
          + COALESCE(s.elapsed_duration_s, s.timer_duration_s, 0) / 86400.0
  );
