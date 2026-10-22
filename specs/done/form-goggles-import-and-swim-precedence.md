---
name: FORM goggles import and swim precedence
id: spec-8912abbd
description: null
dependencies: null
priority: null
complexity: null
status: done
tags: []
scope:
  in: null
  out: null
feature_root_id: B-905515b0
---
# FORM goggles import and swim precedence

## Objective

Import swim sessions recorded by FORM smart swim goggles, match them to the Garmin swims they belong to, and make FORM's more accurate pool-swim data take priority in the swim views.

## Context

- **Platform.** Single-user tool on a Raspberry Pi. Simple code, **no new dependencies**.
- **What FORM provides.** FORM cannot sync to Garmin Connect. Its only export is an email containing a **pre-signed S3 download link**. The link is valid for 48 h, and the link itself is the credential. It points to a ZIP holding one or more FIT files.
- **Verified on a real export.** The FIT file is standard, with `file_id.manufacturer = 'form'` and `product_name = 'FORM Swim Goggles'`. It contains `session` (`pool_length`, `pool_length_unit`, `total_distance`, `num_active_lengths`, `total_moving_time`), `activity` (with `local_timestamp`), `lap` (one per interval), `length` (`active`/`idle`, `swim_stroke` including `drill`) and `record` messages (with HR).
- **Why FORM wins for pool swims.** For the same 50 m swim, the Garmin watch recorded 1,700 m, 34 lengths and 15 min rest. FORM recorded 1,850 m, 37 lengths, 3 drill lengths and 7.1 min rest. The start times were 8 s apart.
- **Privacy.** `file_id.serial_number` is a device serial. It stays in the raw payload only: never copy it into derived tables, logs or output. Tests use synthetic FIT files (`garmin_fit_sdk.Encoder`). The real export stays in gitignored `.local/`.

## Behaviour

1. **`garmin-archive import-form <file.fit|file.zip>... | --url URL`**
   - **Local paths.** Each path can be a `.fit` file or a `.zip` file; shell globs cover folders. ZIPs are read in memory with the existing `processing/fitfile.py:zip_member_contents`, which enforces the safety rules and limits.
   - **`--url`.** Downloads one ZIP over HTTPS.
     - The host must match `^formdata-[a-z0-9-]+\.s3\.[a-z0-9-]+\.amazonaws\.com$`.
     - Never follow redirects. Use finite timeouts and a 64 MiB size cap.
     - **Treat the URL as a secret.** Never write it, or its query string, to the DB, logs, stdout/stderr or exception messages. Report only the host.
     - No Garmin credentials are needed.
   - **Validation.** Each FIT file must decode and have `file_id.manufacturer == 'form'`. Other files are rejected with a per-file reason, so Garmin FIT files cannot be imported by accident.
   - **Idempotence.** Add a dedicated writer method, `ArchiveWriter.record_import(*, body, filename)`. In one transaction it validates `^imported_file:form:[0-9a-f]{16}$` and stores a `raw_payloads` row (`kind = 'imported_file'`, `activity_id = NULL`, `resource_key = 'imported_file:form:<sha256[:16]>'`, content type `application/vnd.ant.fit`) plus one fetch event (method `IMPORT`, the file name as path). It does **not** touch `activity_archive` or `activity_resources`. `record_response` keeps rejecting `imported_file`. If the resource key already exists, nothing is written at all, and the file is counted as `duplicate`.
   - **After importing,** run the FORM processing pass (§2) offline.
   - **Output.** JSON with `imported`, `duplicate`, `rejected` (a list of `{file, reason}`), `matched` and `unmatched`.
   - **Exit codes.** 0 when nothing was rejected. 6 when some files were rejected but others were imported. 2 when nothing valid was given. 4 when the download fails.
2. **FORM processing (part of `reprocess` and `update`, and run after `import-form`).**
   - Rebuild all FORM-derived rows wholesale in one transaction from the `imported_file:form:*` payloads. This is small data. Rows carry `source_payload_id`. There are no `processing_runs` rows, because FORM sessions are not Garmin activities.
   - **`form_sessions`.** One row per FIT session, PK `form_session_id = 'form:' + sha256[:16]`, plus a session index when a file holds more than one session. Columns:
     - `start_at_utc`, `local_date` (from `activity.local_timestamp`), `environment` (`pool`/`open_water`), `pool_length_m`
     - `distance_m`, `active_lengths`, `active_swim_duration_s` (sum of active length timer time), `rest_duration_s` (sum of idle length elapsed time)
     - `timer_duration_s`, `elapsed_duration_s`, `total_strokes`, `avg_hr_bpm`, `max_hr_bpm`
     - `matched_activity_id`, `match_delta_s`, `pool_length_mismatch`
   - **`form_lengths`.** One row per FIT `length`, with the same columns as `swim_lengths`. `distance_m` is the pool length for active lengths and NULL for idle ones. SWOLF is derived as in `swim_lengths`.
   - The pass runs in full on every `reprocess`, including `reprocess --activity-id`, because matching depends on the Garmin rows.
   - **Matching.** A FORM session matches the Garmin swim (sport family `swimming`) with the same environment and the closest start, where |start difference| ≤ 10 min and the two time ranges overlap. Matching is one-to-one. A pool-length disagreement still matches, with `pool_length_mismatch = 1`. Matching is recomputed on every pass, so import order doesn't matter.
3. **Precedence in the swim views** (recreate them in migration `0006`).
   - **`v_swimming`.** Gains `data_source` (`form`/`garmin`) and `garmin_distance_m`, `garmin_active_lengths`, `garmin_active_swim_duration_s`, `garmin_recorded_rest_duration_s`.
     - For **pool** swims with a match, these come from FORM: `distance_m`, `active_lengths`, `total_strokes`, `avg_swolf`, `active_swim_duration_s`, `recorded_rest_duration_s`, `probable_rest_duration_s` (= FORM rest), the paces, and `length_coverage_status = 'complete'`.
     - HR, calories, training effect and every other column stay Garmin.
     - An unmatched FORM session that **does** overlap a Garmin swim (it lost the closest-start contest, or the environments differ) stays in `form_sessions` with `matched_activity_id` NULL and is left out of every view.
     - **Open-water** matches stay Garmin. FORM data is still reachable via `form_*`.
     - For matched pool swims, `avg_distance_per_stroke_m` (= FORM `SUM(distance)` / `SUM(strokes)` over active lengths) and `avg_stroke_rate_spm` come from FORM. `likely_unrecorded_swim_duration_s` is NULL, because FORM's rest replaces the heuristic. `timer_pace_s_per_100m` uses FORM `timer_duration_s` with FORM distance.
     - **FORM-only rows (no double counting).** A FORM session that is unmatched **and overlaps no Garmin swim in time** (any environment) is added as a row, with `activity_id = form_session_id`, `data_source = 'form'`, `type_key = 'lap_swimming'` or `'open_water_swimming'`, `sport_family = 'swimming'`, and NULL for Garmin-only metrics.
   - **`v_swim_lengths`.** For matched pool swims and FORM-only sessions, rows come from `form_lengths`, with `data_source = 'form'` and `likely_unrecorded_swim` NULL. Otherwise they are the Garmin lengths, with `data_source = 'garmin'`. The `activity_id` is the Garmin ID when matched, or the `form_session_id` otherwise.
   - **`v_activity_summary`.** Stays pure Garmin for Garmin activities, and gains `data_source`. **FORM-only sessions, as defined above, are added** as swimming rows (`data_source = 'form'`, `include_in_volume_totals = 1`). Matched swims are **not** overridden here.
   - **View column order.** Migration 0006 lists columns explicitly instead of `a.*` (it needs them for the override and the UNION). Existing columns keep their names and order; new columns are appended.
4. **Schema notes** in the query registry:
   - v_swimming / v_swim_lengths: "Pool swims prefer FORM goggle data when available (`data_source`); Garmin originals are in `garmin_*`. HR, calories and training metrics are always Garmin."
   - v_activity_summary: "Garmin values for Garmin activities, even when FORM data exists, so swim totals here can differ from v_swimming; rows with `data_source = 'form'` are goggle-only swims."
   - form_sessions: "FORM-recorded sessions and their match to a Garmin swim."
5. **SKILL.md.** Add a short section: when the user forwards a FORM export email or link, run `import-form --url '<link>'`, or `import-form <file>` for attachments. Never repeat the link back to the user.

## Acceptance Criteria

Synthetic FORM FIT files are built with `garmin_fit_sdk.Encoder` (`manufacturer='form'`), plus the existing fake Garmin data. No network: `--url` is tested with `httpx.MockTransport`.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`.
2. **Import.** A `.fit` and a `.zip` with two FORM files both import. A second import gives `duplicate` and writes no rows at all (no new `raw_payloads` or `fetch_events` rows). A Garmin FIT file is rejected with a reason. A ZIP with a `..` member is rejected.
3. **Raw storage.** The stored bytes equal the input SHA-256. The serial number appears in no derived table, and in no output or logs.
4. **`--url`.** A mock S3 response imports. A non-allowlisted host, an `http://` URL or a redirect is refused. The URL query string never appears in stdout, stderr, logs or the DB file.
5. **Matching.**
   - A FORM session starting 8 s after a Garmin pool swim matches, with `match_delta_s = 8`.
   - At 11 min apart it does not match.
   - Two FORM sessions near one Garmin swim: only the closest matches, and the other one appears in **no** view, so `v_activity_summary` counts the swim exactly once.
   - A pool-length mismatch is flagged.
   - Importing FORM before syncing Garmin, then running `update`, still matches.
6. **Precedence.**
   - For the matched pool swim, `v_swimming` shows FORM distance, lengths, rest and pace with `data_source = 'form'`, the `garmin_*` columns show the watch values, and HR is Garmin's.
   - `v_swim_lengths` shows FORM's lengths, including drill.
   - `v_activity_summary` shows Garmin's distance for that swim.
   - A matched open-water swim stays Garmin in all views.
7. **FORM-only.** An unmatched FORM session appears in `v_swimming`, `v_swim_lengths` and `v_activity_summary` with `data_source = 'form'` and NULL HR.
8. **Rebuild.** `reprocess` rebuilds the FORM tables identically, with no duplicates.

## Out of scope

- reading email
- FORM cloud APIs
- FORM-only metrics not in the FIT file
- open-water precedence
- deleting imports
- an interval (`form_laps`) table; the raw FIT file is archived
- folder input
