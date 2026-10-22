---
name: Sport metrics and views
id: spec-2699f0ff
description:
dependencies:
priority:
complexity:
status: draft
tags: []
scope:
  in:
  out:
feature_root_id:
---
# Sport metrics and views

## Objective

Add sport-specific metrics for **running, swimming, cycling and cardio**, per-length swim data, and stable query views. These are the views an agent queries (`docs/SPEC.md` §7.2–§7.4). They build on the common tables from "Activity processing: sports and common tables".

## Context

- **Platform.** Single-user tool on a Raspberry Pi. Keep it simple, with **no new dependencies**. Everything is produced by the same per-activity processing pass and atomic replace, under the same processor version.
- **Field facts verified on real files.**
  - **Summary JSON `summaryDTO`.**
    - Units:
      - `averageRunCadence` / `maxRunCadence` are already steps/min.
      - `strideLength` is cm.
      - `groundContactTime` is ms.
      - `verticalOscillation` is cm.
    - Pool length is `poolLength` plus `unitOfPoolLength.unitKey`, e.g. `meter`.
    - Cardio fields: `steps`, `totalSets` and `activeSets` are present. `totalReps` may be null.
  - **FIT.** Running `record.cadence` is **per foot**: steps/min = `(cadence + fractional_cadence) * 2`. FIT step length and vertical oscillation are in **mm**.
  - **Pool swim `length` messages.** `length_type` is `active` or `idle`. `swim_stroke` takes values such as freestyle, breaststroke or none (drill). Timer and elapsed times and `total_strokes` are present.
  - **Missing sensors.** This account's cycling files have no power or cadence data. Those columns stay NULL.

## Behaviour

1. **Metrics tables** (migration `0003`). One row per activity of the matching family, PK/FK `activity_id`, plus `processing_run_id` and `source_payload_id`. All metrics are nullable, and the unit is in each column name.
   - **`running_metrics`** (family `running`): avg/max cadence (spm), avg stride length (m, from cm/100), avg ground contact (ms), avg vertical oscillation (cm), avg vertical ratio (%), avg/max power (W). Source: summary JSON.
   - **`swimming_metrics`** (family `swimming`):
     - `swim_environment`: `pool` for `lap_swimming`, `open_water` for `open_water_swimming`.
     - `pool_length_m`: convert yards ×0.9144. An unknown unit gives NULL plus a warning. Always NULL for open water.
     - active lengths, total strokes, avg SWOLF, avg distance per stroke (m) and avg stroke rate (strokes/min), copied as reported in the summary JSON. Nothing is recomputed locally.
     - `active_swim_duration_s`: the sum of timer time over **active** lengths.
     - `recorded_rest_duration_s`: the sum of **idle** length time.
     - `length_coverage_status`: `none` when the activity has no length records, `complete` otherwise.
     - Do not treat elapsed-minus-timer as rest. Open water has no lengths and no pool length.
   - **`cycling_metrics`** (family `cycling`): avg/max cadence (rpm), avg/max/normalized power (W), training stress score, intensity factor. Source: summary JSON.
   - **`cardio_metrics`** (family `cardio`):
     - `steps` and `total_sets` (from summary JSON)
     - `active_sets`, `total_reps` (sum of `repetitions` over active sets)
     - `active_set_duration_s` and `rest_set_duration_s` (from `activity_sets`)
     - No intervals are invented from heart rate.
2. **`swim_lengths`**: one row per FIT `length` message, **including idle lengths**. PK `(activity_id, length_index)`, with a nullable `lap_index`. Columns:
   - start UTC, `length_type`, `stroke_type`
   - `distance_m`: the pool length for active lengths, NULL for idle
   - timer/elapsed s, stroke count, avg stroke rate
   - `swolf`: derived as timer seconds + strokes, only when both are valid and the length is active
3. **Sample cadence.** For running activities, set `activity_samples.cadence_unit = 'spm'`, with cadence converted by the rule above. For other sports, keep the raw FIT value with unit `fit_rpm`.
4. **Views** (stable names; created in the migration):
   - **`v_activity_summary`**: every activity, with `sport_family` and `include_in_volume_totals` (false for multisport parents, true otherwise).
   - **`v_running`**: running activities joined with `running_metrics`, plus `timer_pace_s_per_km = timer_duration_s * 1000 / distance_m` when distance > 0.
   - **`v_swimming`**: swims joined with `swimming_metrics`, plus `timer_pace_s_per_100m` and `active_pace_s_per_100m`. The active pace is computed only when the active duration is known.
   - **`v_swim_lengths`**: lengths, plus `pace_s_per_100m` for active lengths with distance > 0.
   - **`v_cycling`** and **`v_cardio`**: activities joined with their metrics.

   All sport views exclude multisport parents. Each view has one short header comment stating its grain, units and whether its times include pauses or rests. Paces are never averaged in views. The skill aggregates time ÷ distance.

## Acceptance Criteria

Tests build synthetic FIT bytes with `garmin_fit_sdk.Encoder` and use copies of the committed sanitized summary fixtures. Numeric assertions use a small tolerance. No network.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`.
2. **Running** (summary fixture `running-1`):
   - avg cadence 148.25 spm (not doubled)
   - stride 0.8859 m
   - ground contact and vertical oscillation in their stated units
   - a synthetic record with cadence 74 and fractional 0.125 gives sample cadence 148.25 `spm`
3. **Pool swim**, synthetic FIT, 25 m pool, 6 lengths: 4 active (2 freestyle, 1 breaststroke, 1 drill with no strokes) and 2 idle:
   - 6 `swim_lengths` rows, with idle `distance_m` NULL
   - `active_lengths = 4`; active duration = sum of the active timers; rest = sum of the idle lengths
   - SWOLF NULL for the drill length
   - a 25-yard pool gives `pool_length_m = 22.86`
4. **Open water**: an `open_water` environment, NULL pool length and no `swim_lengths` rows.
5. **Cycling** with no power/cadence: a `cycling_metrics` row exists, with power and cadence NULL (not 0).
6. **Cardio** (summary fixture `cardio-1` plus a synthetic FIT with active/rest sets): `steps` and `total_sets` come from the summary, and reps and set durations are summed from sets.
7. **hiit, walking and other** get no sport-metrics rows, but they appear in `v_activity_summary`.
8. **Views:**
   - `v_running.timer_pace_s_per_km` is correct and NULL when distance is 0.
   - `v_swimming` and `v_swim_lengths` paces are correct.
   - A multisport parent has `include_in_volume_totals = 0` and is absent from the sport views.
   - Every view is queryable after `init` on an empty DB.
9. The atomic-replace test from the processing spec also covers the metrics and length tables: no duplicates after two runs.

## Out of scope

- hiit- and walking-specific metrics
- `metric_provenance`
- `activity_intervals`
- workout-structure parsing
- the query commands
