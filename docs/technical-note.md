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

Live comparison against the **Google Routes API v2** (`computeRoutes`,
travelMode `DRIVE`, routingPreference `TRAFFIC_AWARE`) at the exact same
departure instant our planner is queried at — Wednesday 2026-11-18, 09:00 IST
(rush) and 15:00 IST (quiet). All values are real API responses collected by
`validation/gmaps_routes.py`; detailed table: `docs/od_comparison.json`.
Our side is the live Cloudflare Worker (`/api/journeys`), not a local copy.

| OD pair | Our bus (min) | Google car + traffic (min) | Ratio |
|---|---|---|---|
| Kashmere Gate ISBT → Nehru Place | 54.9 | 46.5 | 1.18× |
| Anand Vihar ISBT → Dhaula Kuan | 56.6 | 70.2 | 0.81× |
| AIIMS → Rohini West | 97.2 | 51.2 | 1.90× |
| IIT Delhi → India Gate | 38 | 21.1 | 1.80× |
| Sarojini Nagar → IHC | 21.3 | 16.8 | 1.27× |
| Kalkaji Mandir → Karol Bagh | 99.7 | 53.1 | 1.88× |

Across all 20 pairs the bus:car ratio at rush hour is **0.81×–2.63×
(median 1.60×)** — physically correct: buses share road space with cars and
add dwell (stops) and signal overhead, which compounds on cross-town
journeys. Note our planner *beats* the car on Anand Vihar → Dhaula Kuan
because the bus takes the direct Ring Road pattern while Google's car route
detours (26.7 km for a 16 km crow-fly). 20/20 pairs answered by the deployed
planner (9 of them require a transfer, which the edge worker rebuilds via
its 1-transfer search).

**Why the Google comparison is driving, not transit (documented honestly):**
the Routes API v2 has **no transit coverage for Delhi** — TRANSIT requests
return a walking fallback (zero transit legs) even for pure metro pairs
such as Kashmere Gate → Rajiv Chowk (3 stops on the Yellow Line). The
legacy Directions/Distance Matrix APIs, which historically carried Delhi
transit, cannot be enabled on new Google Maps projects (HTTP 403
LegacyApiNotActivatedMapError). So the reproducible, audited comparison at
scale is Google's traffic-aware driving time; this limitation (and the
walk-fallback probe) is recorded in `validation/gmaps_routes.py`.

### 7.4 Google Maps Platform integration (product feature)

The planner isn't just *validated* against Google — it now uses Google's
advanced APIs as a first-class part of the product:

* **Places search.** The planner UI searches any Delhi landmark, market,
  mall or hospital, not just the 6,862 DTC stops. Typing shows two groups:
  `🚏 Bus stops` (our GTFS index, instant) and `📍 Places · Google`
  (Places API (New) autocomplete, scoped to a Delhi-NCR bounding box).
  Choosing a place resolves it to coordinates via the Places details
  endpoint and feeds it into the journey search.
* **Live traffic comparison.** Every result set is annotated with Google's
  traffic-aware driving time (Routes API v2, `TRAFFIC_AWARE`) for the same
  departure — the planner displays "🚗 Google Maps drive: X min · 🚌 our
  best bus: Y min · ratio". Verified end-to-end: Nehru Planetarium →
  Select Citywalk Mall renders 61 min by bus vs 56 min by car (1.08×).
* **Key security.** The Google key is stored as a Cloudflare Worker
  *secret* (`GOOGLE_MAPS_KEY`) and is only used server-side inside the
  Worker; the browser never sees it. All Google calls are proxied through
  three documented endpoints: `/api/places/autocomplete`,
  `/api/places/resolve`, `/api/gmaps-drive`.

Two engineering notes for reproducibility: (1) Google's field masks must be
delivered via the `$fields` **query parameter** through Cloudflare Workers —
the `X-Goog-FieldMask` header is silently dropped by the Worker runtime for
these endpoints, which Google surfaces as "FieldMask is a required
parameter"; (2) the Routes API rejects `departureTime` values in the past
(even seconds stale), so the proxy clamps out-of-window departures and lets
Google fall back to live traffic.

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
