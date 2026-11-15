# Delhi Pulse — Technical Note (draft 2, real-data informed)

## 1. What we received and what we built

- **Input:** GTFS-RT VehiclePositions for DTC buses, October 2026, one SQLite
  file per day (`vehicle_feed`: vehicle_id, lat/lng as TEXT, speed, route_id as
  REAL, trip_id, unix timestamp, IST date/time). Archive 16.25 GB, 31 members.
- **Built:** a four-stage pipeline (extract, clean, project, train), a
  stop-pair travel-time model with quantiles and fallback hierarchy, a
  FastAPI journey planner with transfers, headway-based waiting times,
  day-replay ("time travel"), a city speed overlay, and a validation harness.

## 2. Cleaning: what real raw data looks like (Day 13 = Oct 13)

Pipeline stage 2 reads each day's SQLite **directly through DuckDB's sqlite
scanner** (no Python row loops): one parallel columnar SQL statement does
parse-cast, bbox filtering, dedup, and teleport rejection.

Measured on real day 13 (28.7M raw pings -> 17.8M kept, **62.0% kept**):

| reason | rows (approx) |
|---|---|
| duplicate (trip, vehicle, timestamp) keys | ~10.6M |
| teleports (implied >120 km/h AND >150 m jump) | ~0.8M |
| out-of-bbox / junk coordinates | ~0.2M |
| kept | 17.8M |

Cleaning wall time: ~26 s per 28.7M-ping day on a 4-vCPU sandbox
(after rewrite; the naive executemany path took >60 min — the sqlite-scanner
rewrite is ~100x).

Other real-data observations (day 13):
- 81,383 distinct trip labels, 152 median pings per trip (10 s cadence ->
  ~25 min typical trip), mean trip wall span 64.6 min.
- Hourly volume is a textbook Delhi operating day: 1.1-1.3M pings/h in the
  8-11am and 5-8pm peaks, 35k at 3am.
- The feed's own `speed` field is present on 97.7% of rows but its scale is
  unreliable; we rely on *implied* speeds from positions.

## 3. Trip identity: a key discovery about the feed

`trip_id` in the GPS archive is **not** a static GTFS trip id. It is a
runtime label of the form `route_HH_MM[_n]` (e.g. `1795_19_31_6191`),
assigned by the Transport Stack pipeline, i.e. route + scheduled departure
time (+ disambiguator). Static GTFS `trip_id` joins are therefore not
possible by design.

Stage 3 therefore auto-detects between two modes:
- **trips mode** (synthetic fixture / aligned feeds): trip_id in trips.txt.
- **routes mode** (real archive): key on `route_id`, fetch the route's
  candidate shapes (both directions), and for each trajectory **pick the
  best-fitting shape** by median ping-to-shape distance. This is robust to
  missing trip metadata entirely.
- Trajectories are **segmented** at ping gaps >10 minutes (the data shows
  24 h "trip" spans — same label reused across service blocks).

## 4. Projection and edge construction

- Shapes are rasterized to cumulative arc length (equirectangular meters
  local to the network centroid).
- Pings project by nearest-segment (vectorized, chunked); pings >75 m from
  their shape are discarded; progress along the route is made monotone
  (running max), so GPS back-jitter cannot imply reverse travel.
- Stops of the matched pattern project onto the same polyline; ping
  progression interpolates each stop's **passage time**.
- Consecutive passages produce edge traversal samples
  `(route, stop_i, stop_j, dist_m, dur_s, entry_hour)`.

## 5. Travel-time model

Hierarchy with graceful fallback (never a blank):

```
(route, from_stop, to_stop, hour) -> p10/p50/p90 traversal seconds
(route, hour)                     -> observed speed km/h
(route)                           -> observed speed km/h
(hour)                            -> city-wide speed km/h
```

- v0/v1 model: DuckDB `quantile_cont` per edge x hour (>=3 samples);
  route/city aggregates are distance-weighted harmonic-style
  (sum(dist)/sum(dur)).
- Ping-pair speeds (pre-GTFS layer, real data now): pairs with dt<=120 s,
  15 m < d, and 3 < implied speed < 90 km/h — the floor removes depot/creep
  contamination (it pushed per-route averages down by km/h; discovered
  comparing route-hour cells to route-level aggregates).
- **Waiting time** comes from *observed service frequencies* extracted from
  the feed's own trip labels (`route_HH_MM`): distinct labels per
  (route, scheduled-departure hour), averaged across days, -> headway =
  3600/n -> wait = headway/2 clamped to [0.75, 15] min
  (34,448 route-hour frequency cells over the processed days). This model
  works entirely without static GTFS.
- **Dwell is included** in traversal samples (passage-to-passage spans stops),
  and separately observable from stop-linger pings; the brief's "decide
  whether to treat it separately" is answered both ways deliberately:
  end-to-end ETAs include it (that is what riders experience), the model
  decomposition allows reporting it.
- "Usual at a quieter hour" (brief 2.2): each journey option is re-planned
  across commuter-quiet hours (6,7,13-16,22,23) with those hours' own
  headways; the API returns `usual_p50_min`, `usual_hour_ist` and
  `rush_effect_min`. With the ping-pair layer, hour contrasts are muted
  (documented finding: pair speeds miss dwell compounding); the stop-passage
  quantile model restores the fuller effect post-GTFS.

## 6. Ping-pair speeds without GTFS (stage 4f)

Before static GTFS access, route x hour speeds are computed **directly from
consecutive ping pairs** (15 m-90 km/h filter): day 13 alone profiled
**2,112 routes** (27.9k route x hour cells). Use: city speed overlay,
as-of-day replay, and the live-layer primitive (recent window = same
computation on a shorter slice).

Real-data finding: median ping-pair speed is nearly flat across the day
(~18.4 vs 19.1 km/h at 09:00 vs 21:00) because between-ping movement is
dominated by curbside crawl regardless of hour; the *journey-level* rush
penalty accrues through dwell + signal compounding, which the stop-passage
traversal model captures and ping pairs cannot.

## 7. Combining live and historical

One blend path, two sources:
- **live**: GTFS-RT feed (private key, requested from Transport Stack);
  sliding recent window (~15 min) of ping-pair speeds per route.
- **replay**: `?asOf=YYYY-MM-DD` (or timestamp) — the same recent-window
  computation against the October archive, using that day's observed speeds
  (traversals if present, ping-pairs otherwise).

Blend: `est = a x recent + (1-a) x historical`, a = n/(n+k) by recent sample
count; no recent data -> hierarchy fallback + `confidence` flag in the API
response. Replay is the verification story: any October moment can be
re-planned and scored against what actually happened.

## 8. Validation plan and current status

- Edge-replay harness exists (`validation/holdout.py`, reservoir sampling +
  `--test-days` for temporal hold-out): MAE / bias / %-within-5-10-min,
  saved to `model/holdout_report.json`.
- Journey-replay harness exists (`validation/holdout_journeys.py`): real
  observed traversals replayed through the planner, ride-time vs actual,
  plus coverage (did the planner even find it).
- **Measured (real data, leave-one-day-out** `validation/hold_pings.py`,
  178,686 route x hour cells across 7 October days the model never saw):
  route x hour model MAE **2.1 km/h** vs a naive city-hour baseline
  3.13 km/h — a **33% lift**; 92.3% of cells within 5 km/h, 99% within 10.
- Full temporal protocol (train Oct 1-24, test Oct 25-31) executes as soon
  as stop-level traversals exist for real days (static GTFS dependency).
- Google Distance Matrix comparison table + 40 canonical OD pairs: to be
  collected at submission time; the live "Google says" chip is wired for
  it (key configured via .env).

## 9. Known limitations

- Planner walk legs are straight-line x1.33 (OSRM upgrade pending).
- ETA p10/p90 sum per-leg quantiles (independence approximation).
- Loop routes and short-turn variants not yet deduped in routes mode.
- The 7z archive's global SHA256 does not verify on our copy (concurrent
  resume our side caused; members validated individually at extraction).
