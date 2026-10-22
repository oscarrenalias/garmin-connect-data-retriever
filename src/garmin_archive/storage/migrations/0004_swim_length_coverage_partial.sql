-- Allow length_coverage_status 'partial' (SPEC 7.3). DDL only; the runner owns transactions.
-- swimming_metrics rows are rebuildable; they are copied as-is and refreshed by reprocessing.

DROP VIEW v_swimming;

CREATE TABLE swimming_metrics_new (
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
    length_coverage_status TEXT CHECK (length_coverage_status IS NULL OR length_coverage_status IN ('none', 'partial', 'complete'))
);

INSERT INTO swimming_metrics_new SELECT * FROM swimming_metrics;
DROP TABLE swimming_metrics;
ALTER TABLE swimming_metrics_new RENAME TO swimming_metrics;

-- v_swimming: one row per swimming activity (no multisport parents); metres/seconds; timer pace includes rests, active pace uses active_swim_duration_s only (rests excluded) and is NULL when unknown or distance_m <= 0. Per-length data reflects the device recording; for partial coverage, totals come from the (possibly edited) summary.
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


-- v_swim_lengths: one row per pool length (active and idle; no multisport parents); metres/seconds; timer_duration_s is per-length timer time, pace_s_per_100m only for active lengths with distance_m > 0 (rest lengths excluded). Per-length data reflects the device recording; for partial coverage, totals come from the (possibly edited) summary.
DROP VIEW v_swim_lengths;
CREATE VIEW v_swim_lengths AS
SELECT
    l.*,
    a.local_date,
    CASE WHEN l.length_type = 'active' AND l.distance_m > 0
        THEN l.timer_duration_s * 100.0 / l.distance_m END AS pace_s_per_100m
FROM swim_lengths AS l
JOIN activities AS a ON a.activity_id = l.activity_id
WHERE COALESCE(a.is_multisport_parent, 0) = 0;
