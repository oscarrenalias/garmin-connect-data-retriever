---
name: "Activity processing: sports and common tables"
id: spec-939594b5
description: null
dependencies: null
priority: null
complexity: null
status: done
tags: []
scope:
  in: null
  out: null
feature_root_id: B-97ab5d52
---
# Activity processing: sports and common tables

## Objective

Turn the raw archive into rebuildable analytical tables that every activity gets, whatever its sport. Sport-specific metrics and the query views come in the next spec ("Sport metrics and views"). See `docs/SPEC.md` §7.

## Context

- **Platform.** This is a single-user tool that runs on a Raspberry Pi. Keep the code simple and add **no new dependencies**. FIT files are decoded with the existing `garmin-fit-sdk`.
- **Inputs.** Everything is read from what sync already archived: the current `summary` payload (detail JSON), the `search_item` (list JSON) and the `original_file` payload (a ZIP containing one `.fit`). Processing is **offline**: no network and no credentials. It never modifies raw payloads.
- **Facts verified on real files from this account:**
  - Every sport's FIT file has `session` and `record` messages.
  - Running, swimming, walking and cycling files have `lap` messages. Cardio, hiit, strength and mobility files have **no laps** but do have `set` messages (repetitions, category, duration).
  - Pool swims have `length` messages.
  - The SDK returns timestamps as datetimes. Positions are semicircles, so `deg = v * 180 / 2**31`. Speeds are m/s (`enhanced_speed`) and altitude is m (`enhanced_altitude`).
  - Summary JSON `startTimeGMT` has no offset and is UTC. `timeZoneUnitDTO.timeZone` is an IANA zone.

## Behaviour

1. **Sports list (`processing/sports.py`).** A plain dict maps a Garmin `typeKey` to a family. It carries a `SPORTS_VERSION` constant.

   | Family | Type keys |
   | --- | --- |
   | `running` | `running`, `treadmill_running`, `trail_running`, `track_running`, `street_running`, `indoor_running`, `virtual_run` |
   | `swimming` | `lap_swimming`, `open_water_swimming` |
   | `hiit` | `hiit` |
   | `cardio` | `indoor_cardio` |
   | `cycling` | `cycling`, `road_biking`, `mountain_biking`, `gravel_cycling`, `indoor_cycling`, `virtual_ride` |
   | `walking` | `walking`, `casual_walking`, `speed_walking` |

   Every other key, including unknown ones, maps to `other`. Never infer the sport from the activity name.
2. **Schema.** Migration `0002` adds the tables below. Every derived row carries `processing_run_id` and `source_payload_id`. Metrics are nullable, because absence is not zero. Garmin IDs are TEXT. UTC timestamps are RFC 3339 strings ending in `Z`. Column names carry a unit suffix.
   - **`activities`**: one row per archived activity, PK `activity_id`. Columns follow SPEC §7.2: parent, multisport flag, `type_key`, `sport_family`, name, description, start UTC/local, `local_date`, timezone, distance, the timer/elapsed/moving durations, avg/max speed, avg/max HR, calories, elevation gain/loss, aerobic/anaerobic training effect, training load, `processing_status`, `processed_at_utc`.
     - Source order is summary JSON (`summaryDTO`, `activityTypeDTO`, `timeZoneUnitDTO`, `parentId`, `isMultiSportParent`), then the search item.
     - `local_date` = the first 10 characters of `startTimeLocal`.
     - `name` and `description` are stored as data and never interpreted.
   - **`activity_sessions`**: one row per FIT `session` message. PK `(activity_id, session_index)`. Columns: FIT sport/sub-sport, start UTC, distance, timer/elapsed, avg/max HR, calories.
   - **`activity_laps`**: one row per FIT `lap` message. PK `(activity_id, lap_index)`, with a `session_index` column. Columns: start UTC, distance, timer/elapsed, avg/max HR, avg speed, `lap_trigger`, `intensity`.
   - **`activity_samples`**: one row per FIT `record` message, at **native rate** with no resampling and no filled gaps. PK `(activity_id, sample_index)`. Columns:
     - `timestamp_utc` and `elapsed_s` (from the first record)
     - `distance_m`, `speed_mps`, `hr_bpm`, `altitude_m`
     - `latitude_deg`, `longitude_deg`
     - `power_w`, `temperature_c`
     - `cadence` (raw FIT value) and `cadence_unit` (`fit_rpm`). The sport spec converts cadence.
   - **`activity_sets`**: one row per FIT `set` message. PK `(activity_id, set_index)`. Columns: `set_type` (active/rest), start UTC, `duration_s`, `repetitions`, `weight_kg`, `category` (raw).
3. **FIT handling.**
   - Read the ZIP in memory. Reject members with absolute paths or `..`, and enforce fixed size and entry-count limits (module constants).
   - Decode the first `.fit` member with the SDK.
   - A missing file, a non-ZIP body or a decode error does not fail the activity. It is processed from JSON only, with `processing_status = 'partial'` and a warning in its processing run.
   - A multisport parent's FIT sessions are stored on the parent only. Never copy them to the children.
4. **Processing run and atomic replace.**
   - For each activity, insert a `processing_runs` row (`processor_version` = package version + `SPORTS_VERSION`, `input_fingerprint` = hash of the current summary and file payload IDs + processor version).
   - Delete and re-insert all of that activity's derived rows in **one transaction**.
   - On an unexpected error, roll back, so the previous rows stay. Record the run as `failed` and continue with the next activity.
   - Use `executemany` for samples. A Pi must cope with about 500k sample rows overall.
5. **When processing runs.**
   - At the end of every `update`, process every activity whose `input_fingerprint` differs from its last `processed` or `partial` run, so a partial activity is not retried until its inputs change. That covers new activities, new payload versions and a new processor version. The search item is deliberately left out of the fingerprint, because it is only a fallback source.
   - `update` output gains `processed` and `processing_failed` counters.
   - A processing failure never changes the sync status. Archive and processing are independent outcomes (SPEC §6.2).
6. **`garmin-archive reprocess`.** Offline rebuild of all activities, or one with `--activity-id`. It runs regardless of fingerprints, holds the writer lock, and prints JSON counters: processed, partial, failed. Exit codes: 0 on success, 7 if any activity failed, 5 for storage errors.

## Acceptance Criteria

Tests build synthetic FIT bytes in memory with `garmin_fit_sdk.Encoder`, which is installed and round-trips session, lap, record, set and length messages. They also use copies of the committed sanitized summary fixtures. Numeric assertions use a small tolerance (the SDK rounds scaled fields such as altitude). No real activity files are committed, and there is no network.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`.
2. Every type key in the table maps to its family. `mobility`, `strength_training` and an unknown key map to `other`. An activity named "Morning Run" with type `cycling` is `cycling`.
3. A running activity (summary fixture + synthetic FIT with 1 session, 3 laps and 100 records with GPS) produces:
   - 1 `activities` row with the summary values, correct UTC and local times, `local_date` and timezone
   - 1 session row, 3 lap rows, 100 sample rows
   - latitude in degrees within 1e-6 of the expected value
   - absent fields as NULL, never 0
4. A cardio activity whose FIT has no laps and 10 set messages produces 10 `activity_sets` rows and 0 lap rows.
5. A corrupt ZIP, a missing file or an undecodable FIT gives `processing_status = 'partial'` with the `activities` row still filled from JSON. The raw payload is unchanged (same SHA-256).
6. Re-running processing with unchanged inputs does nothing. A new summary payload version or a bumped processor version reprocesses that activity, and the old derived rows are fully replaced (no duplicates or leftovers).
7. A failure injected mid-replace leaves the previous derived rows intact, and the run is `failed`.
8. A multisport parent and its children: the parent's FIT sessions are stored on the parent only.
9. `update` (against the existing fake Garmin API) processes the activities it fetched and reports the processing counters. A processing failure leaves the sync status `complete`.
10. `reprocess` and `reprocess --activity-id` work offline: a fake transport that fails on any request proves no network is used. They print one JSON document with the documented exit codes.

## Out of scope

- sport-specific metrics, swim lengths and views (next spec)
- `metric_provenance` and `activity_intervals` tables
- FIT fallback for summary metrics
- the query commands
