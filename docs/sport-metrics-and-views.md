# Sport metrics tables and stable views

Reference for migration `0003_sport_metrics.sql` (SPEC §7.2–§7.4, as amended by §7.3a). All tables are derived and rebuildable by `reprocess`. Every row carries `processing_run_id` and `source_payload_id`. Metric columns are nullable: NULL means absent, never 0.

## Tables

Sport metric tables are keyed by `activity_id` (TEXT, PK and FK to `activities`). Summary metrics come from the detail JSON `summaryDTO` (`processing/metrics.py`).

| Table | Grain | Columns and units |
| --- | --- | --- |
| `running_metrics` | One row per running activity | `avg_cadence_spm`, `max_cadence_spm` (steps/min), `avg_stride_length_m`, `avg_ground_contact_time_ms`, `avg_vertical_oscillation_cm`, `avg_vertical_ratio_pct`, `avg_power_w`, `max_power_w` |
| `swimming_metrics` | One row per swim | `swim_environment` (`pool`, `open_water` or NULL), `pool_length_m` (pool only), `active_lengths`, `total_strokes`, `avg_swolf`, `avg_distance_per_stroke_m`, `avg_stroke_rate_spm`, `active_swim_duration_s`, `recorded_rest_duration_s`, `length_coverage_status` (`none`, `partial` or `complete`) |
| `cycling_metrics` | One row per cycling activity | `avg_cadence_rpm`, `max_cadence_rpm` (revolutions/min), `avg_power_w`, `max_power_w`, `normalized_power_w`, `training_stress_score`, `intensity_factor` |
| `cardio_metrics` | One row per cardio activity | `steps`, `total_sets`, `active_sets`, `total_reps`, `active_set_duration_s`, `rest_set_duration_s` |
| `swim_lengths` | One row per FIT length, idle included; PK `(activity_id, length_index)` | `lap_index` (NULL if no lap window matches), `start_at_utc`, `length_type`, `stroke_type`, `distance_m`, `timer_duration_s`, `elapsed_duration_s`, `stroke_count`, `avg_stroke_rate_spm`, `swolf` |

### Swim semantics

- `swim_lengths` come from FIT `length` messages in message order. Idle lengths are kept; their `distance_m` is NULL, so they add no swimming distance. Active `distance_m` is the pool length.
- `swolf` is derived (timer seconds plus strokes) and only set for active lengths with a valid timer time and a positive stroke count. `avg_swolf` in `swimming_metrics` is the value Garmin reports.
- `active_swim_duration_s` and `recorded_rest_duration_s` are sums of length timer seconds by `length_type` (`active` / `idle`). They are never computed as elapsed minus timer. Both are NULL when the activity has no length records.
- `length_coverage_status` is `none` without length records. It is `partial` when the active-length count differs from the summary `numberOfActiveLengths`, or when active-length distance differs from the summary distance by more than half a pool length (typically lengths corrected in Garmin Connect after recording). Otherwise it is `complete`. Per-length data reflects the device recording; for partial coverage, totals come from the (possibly edited) summary.
- Open-water swims get no pool length and no synthetic lengths.

### Cadence unit rule

- Summary cadence (`averageRunCadence`, `maxRunCadence`) is already steps/min and is stored as is, never doubled.
- FIT sample and lap cadence is stored raw with `cadence_unit = 'fit_rpm'`. Only for running is it converted to steps/min as `(cadence + fractional_cadence) * 2`, with `cadence_unit = 'spm'`.
- Cycling cadence is revolutions/min (`_rpm` columns), not steps/min. Do not compare it with running `_spm` columns.

## Views

All views expose `activities.*` plus the columns below. Distances are metres, durations seconds, speeds m/s, heart rate bpm. `timer_duration_s` excludes pauses. `elapsed_duration_s` includes them.

| View | Grain | Added columns |
| --- | --- | --- |
| `v_activity_summary` | One row per activity, including multisport parents | `include_in_volume_totals` (0 for multisport parents, else 1) |
| `v_running` | One row per running activity | all `running_metrics` columns, `timer_pace_s_per_km` |
| `v_swimming` | One row per swimming activity | all `swimming_metrics` columns, `timer_pace_s_per_100m`, `active_pace_s_per_100m` |
| `v_swim_lengths` | One row per swim length, active and idle | all `swim_lengths` columns, `local_date`, `pace_s_per_100m` |
| `v_cycling` | One row per cycling activity | all `cycling_metrics` columns |
| `v_cardio` | One row per cardio activity | all `cardio_metrics` columns |

### Multisport exclusion

Multisport parents appear only in `v_activity_summary`. Every sport view and `v_swim_lengths` filters them out (`COALESCE(is_multisport_parent, 0) = 0`), so parent and child activities are never double counted. When summing volume from `v_activity_summary`, filter on `include_in_volume_totals = 1`. The sport views are inner joins on the metrics table, so an activity appears there only after it has been processed with a metrics row.

### Pace and rest/pause semantics

| Column | Definition | Rests / pauses | NULL when |
| --- | --- | --- | --- |
| `v_running.timer_pace_s_per_km` | `timer_duration_s * 1000 / distance_m` | Pauses excluded | `distance_m` is not > 0 |
| `v_swimming.timer_pace_s_per_100m` | `timer_duration_s * 100 / distance_m` | Includes recorded rests within timer time | `distance_m` is not > 0 |
| `v_swimming.active_pace_s_per_100m` | `active_swim_duration_s * 100 / distance_m` | Rests excluded | `active_swim_duration_s` is NULL or `distance_m` is not > 0 |
| `v_swim_lengths.pace_s_per_100m` | `timer_duration_s * 100 / distance_m` | Active lengths only; idle lengths are NULL | not an active length, or `distance_m` is not > 0 |

Paces are never averaged. To aggregate, divide total time by total distance, for example `SUM(timer_duration_s) * 1000.0 / SUM(distance_m)`. `AVG(timer_pace_s_per_km)` weights a 1 km run the same as a marathon.

## SPEC divergences (flagged, `docs/SPEC.md` not edited)

The implementation differs from SPEC §7.2–§7.4 in the following ways. §7.3a already records the cycling, cardio-column and primary-key deviations. The rest are not yet recorded there and need a decision from the spec owner:

1. `running_metrics.avg_ground_contact_time_ms` is the implemented name. SPEC §7.2 says `avg_ground_contact_ms`.
2. `swimming_metrics.swim_environment` allows `pool`, `open_water` or NULL. SPEC §7.2 lists `unknown`.
4. `avg_distance_per_stroke_m` reads `averageStrokeDistance`. SPEC §7.3 names `strokeDistance`. `avg_stroke_rate_spm` reads `averageSwimCadence`, which the SPEC mapping table doesn't list.
5. `swim_lengths` has no `session_index` in its key. §7.3a covers this for laps, samples and lengths.
6. `v_archive_coverage` (SPEC §7.4) is not built. §7.3a covers this. The metrics tables and views do not yet carry the `metric_provenance` rows from SPEC §7.2.
7. SPEC §7.4 says the `include_in_volume_totals` flag is "false/true". The view returns integers 0 and 1.
