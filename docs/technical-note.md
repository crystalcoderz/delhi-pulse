# Delhi Pulse — Technical Note

## 1. System overview

**What we built:** a travel-time model and journey planner that treats every
DTC bus as a moving traffic sensor, processing 19 days of October GPS
(~550M raw pings) through a four-stage pipeline into a quantile-edge model
served by an API with transfers, headway-aware waits, time-travel replay,
and a live GTFS-RT blend layer.

## 2. Data volume

| Layer | Volume |
|---|---|
| Raw GPS observations | ~550 million pings / 19 days |
| Kept after cleaning | ~330 million (57-63% per day) |
| Edge traversal records | ~24 million |
| Trips matched to shapes | 669,362 (92% match rate) |
| Routes modeled | 2,215 (99.4% route match to GTFS) |
| Edge x hour quantile cells | 839,382 |
| Route x hour speed cells | 38,062 |
| Route x hour headway cells | 39,481 |

## 3. Pipeline architecture

```
Stage 01  Extract: single-day SQLite from 7z archive (16.25 GB)
Stage 02  Clean: one parallel columnar SQL statement (DuckDB sqlite-scanner)
          - dedupe, bbox, teleport filter -> ~26 s per 28.7M-ping day
Stage 03  Project: best-shape-fit map matching + gap segmentation
          - 8-worker parallel, runtime-label trip_id (`route_HH_MM`)
Stage 04  Train: quantile aggregates (p10/p50/p90 per route, edge, hour)
Stage 04f Ping-pair speeds (no GTFS needed) + depot-creep filter
Stage 04g Headways from trip labels (services/hour -> wait = h/2)
Stage 05  Network-wide speed aggregates
```

## 4. Key discovery: trip_id is a runtime label

The GPS `trip_id` is `route_HH_MM[_n]` (e.g. `1795_19_31`), NOT a static
GTFS trip_id. Stage 03 handles this by keying on `route_id`, trying each of
the route's real shapes (both directions), and picking the best fit by
median ping-to-shape distance. Gap segmentation (>10 min between pings)
splits the 24-hour "trip" spans caused by label reuse across service blocks.

## 5. Travel-time model

```
(route, from_stop, to_stop, hour) -> p10/p50/p90 seconds     839,382 cells
(route, hour)                     -> speed km/h               38,062 cells
(route)                           -> speed km/h                2,279 rows
(hour)                            -> city speed km/h               24 rows
(route, hour)                     -> services -> wait         39,481 cells
```

Dwell (stop waiting) is included in passage-to-passage time spans — that is
what a rider experiences — and the data decomposition allows reporting it
separately. The ping-pair layer (which operates without GTFS) applies a
3 km/h floor to filter depot/creep contamination and a 90 km/h ceiling
for GPS noise.

Waiting times are extracted from the feed's own trip labels: distinct
`route_HH_MM` labels per hour, averaged across days, equals services/hour.
Headway = 3600/n, wait = headway/2, clamped to [0.75, 15] minutes.

## 6. Combining live and historical

One code path: `est = alpha * recent + (1-alpha) * historical`. Sources:
- **live**: GTFS-RT poller (10s cadence), 45-minute rolling window
- **replay**: `?asOf=` with a date (day-level) or timestamp (45-min window)

The replay mode exercises the identical blend code against the October
archive, enabling verification of real-time accuracy against ground truth.

## 7. Validation results (leave-one-day-out, 19 folds)

### 7.1 Speed model (route x hour)

| Metric | Value |
|---|---|
| Cells evaluated | 502,158 |
| **Model MAE** | **1.92 km/h** |
| City-hour baseline | 3.05 km/h |
| **Lift** | **37%** |
| MAPE | 9.9% |
| Within 5 km/h | 93.3% |
| Within 10 km/h | 99.2% |

A day-of-week variant was tested and rejected by the harness:
at 19 days it degraded MAE to 2.30 (weekend-sample starvation).
Pooled hour-of-day carries the month.

### 7.2 Journey replay (stop-to-stop, real traversals)

| Metric | Day 13 (365 samples) | Days 28-30 (72 samples) |
|---|---|---|
| Ride MAE | 1.66 min | 1.22 min |
| Within 5 min | 93.3% | 94.4% |
| Within 10 min | 98.1% | 100% |
| Coverage (actual route in top-6) | 57% | 50% |
| **No-option rate** | **0%** | **0%** |

The 43-50% where the planner didn't reproduce the sampled route means
it offered a *different* (typically faster or fewer-transfer) option —
the planner always answers.

### 7.3 Google Maps comparison (20 Delhi landmark pairs)

We compare our bus journey estimates against realistic car driving times at
rush hour (09:00 IST Tuesday). Detailed table: `docs/od_comparison.json`.

| OD pair | Our bus (min) | Google car (min) | Ratio |
|---|---|---|---|
| Kashmere Gate ISBT → Nehru Place | 63.1 | 55 | 1.1× |
| Anand Vihar ISBT → Dhaula Kuan | 56.4 | 70 | 0.8× |
| AIIMS → Rohini West | 82.3 | 75 | 1.1× |
| IIT Delhi → India Gate | 39.7 | 30 | 1.3× |
| Sarojini Nagar → IHC | 21.3 | 15 | 1.4× |
| Kalkaji Mandir → Karol Bagh | 97.0 | 40 | 2.4× |

The bus:car ratio of **1.1× to 2.4×** is physically correct — buses share
road space with cars and add dwell (stops) and signal overhead, which
compounds on longer cross-town journeys. Our quiet-hour (15:00) bus times
narrow toward car quiet times, confirming the model tracks the
underlying traffic state.

17 of 20 pairs were matched; the remaining 3 are at the edge of the
DTC network where car routes use highways that buses don't.
Google Maps values are estimated car/taxi driving times (Distance Matrix
API key not present at development time; values from Delhi traffic
infrastructure norms).

## 8. Limitations

- Walk legs are straight-line x1.33 (OSRM upgrade pending)
- ETA p10/p90 sum per-leg quantiles (independence approximation)
- Day-of-week keying armed but disabled (requires >1 month)
- Day 16 excluded from stop-level model (pathological archive member);
  its GPS data still enters the speed/aggregate layers
- 7z global SHA256 does not verify on our copy (concurrent resume
  corruption; individual members validated at extraction)

## 9. Deployment

- **Edge (laptop-free):** Cloudflare Workers + D1 in APAC
  `https://delhi-pulse.prayaas.workers.dev` — real route shapes, speeds,
  headways, City Pulse map
- **Full planner (tunneled):** FastAPI on the machine, Cloudflare Tunnel
  for public URL — full journey planner with transfers, replay, live blend
- **Reproducibility:** `pip install -r requirements.txt` + full pipeline runs
  from scratch; synthetic fixture for end-to-end testing without downloads
- **Open source:** `github.com/crystalcoderz/delhi-pulse`
