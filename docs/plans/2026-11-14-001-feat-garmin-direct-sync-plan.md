---
title: Garmin direct sync + ride-analysis MCP tools
status: Stage A implemented (U1-U6 done); deploy pending user approval
date: 2026-11-14
branch: feat/garmin-direct (off feat/claude-app-mcp)
---

# Garmin direct sync + ride-analysis MCP tools

## Problem

All post-export data reaches the coach through intervals.icu. It is slow (today's 10:13 ride was in
Garmin Connect, not in intervals at 13:50) and lossy (no laps, zone times, FIT records).
The planned `coach/integrations/garmin.py` (plan 2026-11-03, U5) was never built.

## Probe results (2026-11-14, read-only, official Personal API)

- Auth: API key -> bearer token, **24 h lifetime**, no refresh token. Cache to disk, re-exchange on expiry/401.
- `search_activities(start_date,end_date)`: today's ride present (id 25074559291, road_biking,
  30 km, 620 m, 1h47). Summary carries NP, IF, TSS, avg/max power, `maxAvgPower_{1..7200}`,
  `hrTimeInZone_1..5`, `powerTimeInZone_1..7`, training load, TE, lapCount.
- `get_activity(id)`: summaryDTO incl. `functionalThresholdPower` (159), totalWork, L/R balance, splits.
- `download_activity(id)`: ZIP with original FIT (434 KB): 1 s `record`s, `lap`, `session`,
  `time_in_zone`, `zones_target` (FTP 159, LTHR 166, max HR 202), `user_profile` (weight 84.5),
  `climb_pro`, `hrv`. **Nothing is stripped.**
- `get_heart_rate_zones()`: DEFAULT + CYCLING floors, LTHR, max HR.
- Client is `garmin-connect-personal-api-python` **1.0.0**. Has sleep, HRV, body battery, daily
  summaries, stress, steps, weight, HR zones, activities, generic `get()`. **No** VO2max, training
  readiness, training load endpoints. No FTP/weight in profile settings -> from FIT/activity.
- Data-quality flag: FTP 159 W vs NP 178 W over 107 min (IF 1.12) => Garmin FTP stale.
  Max HR disagrees (185/195/201/202). Tools expose source of each threshold.

## Key decisions

- KD1 Garmin Personal API is **primary** for Garmin-recorded activities and wellness. intervals.icu
  stays for Zwift-only rides and plan push. Remove unofficial `garminconnect` dep (KTD7).
- KD2 Thin wrapper `coach/integrations/garmin.py` around the official client; tests fake the client class.
- KD3 Store gzipped raw FIT in `settings.fit_dir` (default `/data/fit` in container), derive
  everything server-side; re-derivable.
- KD4 Pure analysis functions (`coach/analysis/ride.py`) over the 1 s stream. Garmin NP/IF/TSS stored;
  computed values labelled `computed_*` in tool output.
- KD5 Thresholds: Stage A stores `ftp_used` + weight/LTHR/max HR per activity (from FIT);
  `get_athlete_settings(date?)` returns latest values on/before date with source. Full history table
  deferred to Stage B.
- KD6 Dedup (revised): intervals already writes `garmin_activity_id` from `external_id`
  (intervals_sync.py `_garmin_id`). Match order: (1) `garmin_activity_id`; (2) `start_utc` ±300 s
  (same as `MATCH_TOLERANCE_S`) among rows with `garmin_activity_id IS NULL`. On match UPDATE in place,
  set `source='garmin_api'`, keep `icu_activity_id`. Never DELETE/merge rows (stream cascade,
  `planned_session.matched_activity_id` FK). intervals sync already only gap-fills non-intervals rows.
- KD7 `garmin_export` re-import must not downgrade `garmin_api` rows (skip/COALESCE on conflict).

## Units — Stage A (tools 1, 2, 3, 4, 7, 8)

**Progress:** U1 done (fbf98db), U2 done (a6cfc78), U3 done (74a36e9), U4 done (87cf273),
U5 done (8fffa25), U6 done (commit "U6: Stage A MCP tools"). Deploy pending the user's approval.

**U0 Deps & config** — `pyproject.toml`, `Dockerfile` unchanged, `coach/config.py`.
- Move `fitdecode` to core deps (prod image runs `pip install .` without extras); add
  `garmin-connect-personal-api-python>=1.0,<2`; remove `garminconnect`.
- Settings: `garmin_token_cache` (default `/data/garmin_token.json`), `fit_dir` (default `/data/fit`),
  created at startup.

**U1 Garmin client** — `coach/integrations/garmin.py`.
- Token cache shared by app + MCP processes: flock during exchange, atomic write (tmp + `os.replace`),
  mode 0600. Re-auth once on 401; backoff on 429/5xx (max 3); per-request timeout; raise
  `GarminUnavailable`.
- Methods: `list_activities`, `activity`, `fit` (unzipped bytes), `hr_zones`, `sleep`, `hrv`,
  `body_battery`, `daily_summary`, `weight`.
- Tests: fake client class; cache reuse/expiry/401 retry/429 backoff; key/token absent from repr & logs.

**U2 Migration 005** — `coach/db/migrations/005_garmin_direct.sql` (**needs explicit approval**).
- `activity` + `intensity_factor`, `tss`, `work_kj`, `ftp_used`, `lthr_used`, `max_hr_used`,
  `weight_kg_used`, `fit_path` (reuse existing `norm_power`, `training_load`, `aerobic_te`, `raw_json`).
- `activity_lap(activity_id FK CASCADE, idx, start_utc, duration_s, distance_m, avg_power,
  norm_power, avg_hr, max_hr, ascent_m, descent_m, trigger)`, PK(activity_id, idx).
- `activity_zone_time(activity_id FK CASCADE, kind, zone, seconds, low, high)`, PK(activity_id, kind, zone).
- Wrapped in BEGIN/COMMIT with the schema_migrations insert inside (or make `migrate.py`
  transactional for all future migrations). Bump `REQUIRED_SCHEMA_VERSION` to 5 (server.py:27).
- Test: apply on fresh DB and on a copy of the prod schema; failure leaves version 4 intact.

**U3 FIT parsing** — `coach/ingest/garmin_export.py` (extend).
- `parse_fit_ride` currently returns only `(start_epoch, records)`. Extend to `FitRide` with laps,
  session totals, time_in_zone, zones_target, user_profile; existing callers unaffected.
- Define pause handling: gaps > 5 s = pause (excluded from NP/segments), ≤ 5 s resampled as 0 W.
- Tests: synthetic FIT (fitdecode-encoded fixture or tiny committed file), paused-ride case.

**U4 Garmin sync** — `coach/ingest/garmin_sync.py`, `coach/jobs/sync.py`, `coach/jobs/scheduler.py`.
- `sync(conn, client, now, since=None, deadline=None) -> SyncResult(new, updated, fit_pending, errors)`.
  Cursor `sync_state['garmin_activities']` (overlap 2 days). Upsert per KD6. Fetch detail + FIT only
  for new rides / rides without `fit_path`; stop FIT downloads at deadline (picked up next run).
  One transaction per activity: row, stream, laps, zones, power bests.
- Cross-process lock in `sync_state` (with stale timeout) shared by job, `sync_now`, backfill.
- Wellness mappers (API JSON -> `wellness_day`, `weight` source `garmin_api`): sleep, HRV, body
  battery, RHR, weight. Call `derived.mark_sleep_seen`. Change intervals wellness to gap-fill only
  (`COALESCE(existing, excluded)`) for Garmin-owned columns when Garmin is enabled.
- KD7 fix in `garmin_export.import_activities`.
- Scheduler: separate `garmin_client_factory`; `garmin_sync` job every 15 min, max_instances=1,
  independent of intervals client.
- Backfill CLI `python -m coach.ingest.garmin_sync --since 2026-11-01`, same lock/dedup path.
- Tests: new ride; intervals row already carrying garmin id; intervals row with NULL garmin id within
  300 s; intervals re-sync after Garmin doesn't regress fields; export re-import keeps `garmin_api`;
  FIT failure -> summary row + retry; second backfill = 0 new; deadline leaves `fit_pending`.

**U5 Analysis core** — `coach/analysis/ride.py` (pure).
- `np`, `work_kj`, `zone_time`, `drift` (Pa:HR halves), `segment(stream, by, from, to)` for
  time | distance | altitude (first crossing of `from`, then first subsequent crossing of `to`) | lap.
  Returns duration, distance, gain, avg/NP power, W/kg, VAM, avg/max HR, zone time, % above `above_w`, drift.
- Tests: table-driven synthetic streams; NP within 1 W of Garmin on the real ride (local-only).

**U6 MCP tools** — done: `coach/mcp/tools_ride.py` (new), wired in `coach/mcp/server.py`;
tests in `tests/test_mcp_tools_ride.py`.
- `sync_now()` (write): Garmin sync last 2 days, internal 45 s deadline, min interval 2 min stored
  in `sync_state`, respects lock; falls back to intervals sync if Garmin unavailable; says which.
- `get_athlete_settings(date?)`: KD5 values + source + stale-FTP hint (best 20-min × 0.95 > FTP).
- `get_activity(id, id_kind='internal'|'garmin'|'icu')`: summary, computed metrics, laps, zone time.
- `analyze_segment(id, by, from, to, above_w?, id_kind)`.
- `get_power_curve(start, end, durations?)`: durations validated against `POWER_BEST_DURATIONS`.
- `get_streams(id, fields, resolution_s=10, start_s, end_s)`: cap ~300 points, returns
  `truncated` + `next_start_s`, via `envelope()`.
- Tests: seeded DB; response-size assertion (< ~6k chars default).

## Stage B

- Events: `set_event`/`get_events`, `next_event` in `get_today`/`get_plan` (goal table + `priority`,
  `key_climbs_json`).
- `athlete_threshold` history table.
- Segments: `save_segment`, auto-matching on sync (start/end within 30 m, distance ±10 %),
  `get_segment_history`.
- VO2max / training readiness via generic `get()` once endpoint paths are probed.

## Rollout

1. Branch `feat/garmin-direct` in `catone:~/coach-dev`; TDD per unit; full suite + ruff/mypy green.
2. Ask approval for migration 005 + deploy. Back up `/opt/coach/data/coach.sqlite`.
3. Deploy to `/opt/coach/app`: `docker compose build` then `up -d --force-recreate`.
4. Backfill `--since 2026-11-01`; verify on prod DB: ride 25074559291 has 6 laps, ~6435 stream rows,
   zone rows, `ftp_used=159`; no duplicate activity rows; `planned_session` matches intact.
5. Call MCP tools from Claude against prod; compare to Garmin Connect numbers.
6. Stage B after a week of Stage A use.

## Risks

- Undocumented rate limits: FIT only for new rides; 15-min cadence; backoff.
- Zwift rides: if Zwift uploads to Garmin, KD6 matches; else intervals path remains.
- Wellness flapping between sources: mitigated by gap-fill rule in U4.

## Open items for the athlete

- Update FTP in Garmin (159 W looks stale) and reconcile max HR.
- Siena event details (Stage B).
