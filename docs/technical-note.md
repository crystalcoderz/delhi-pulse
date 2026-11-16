# Delhi Pulse — Technical Note

## 1. System overview

**What we built:** a travel-time model and journey planner that treats every
DTC bus as a moving traffic sensor, processing **31 days of October GPS**
(912.9M raw pings, 560.6M kept) through a four-stage pipeline into a
quantile-edge model — **853,480 (route, edge, hour) cells with real
p10/p50/p90 quantiles, trained on 39.2M edge traversals from 1,073,526
observed bus trips, covering 2,245 served routes (2,303 profiled in the
speed layer)** — exposed by an API with transfers, headway-aware waits,
time-travel replay, and a live GTFS-RT layer in the local API (the edge
Worker serves the pooled full-month model — see §6).

## 2. Data volume

| Layer | Volume |
|---|---|
| Raw GPS observations | 912.9 million pings / 31 days |
| Kept after cleaning | 560.6 million (56.4-66.3% per day) |
| Edge traversal records | 39.2 million |
| Trips matched to shapes | 1,073,526 (92% match rate) |
| Routes | 2,303 in the ping-pair speed layer — **2,245 served in the edge model** (99.4% route match to GTFS) |
| Edge x hour quantile cells | 853,480 |
| Route x hour speed cells | 40,211 |
| Route x hour headway cells | 41,844 |

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
(route, from_stop, to_stop, hour) -> p10/p50/p90 seconds     853,480 cells
(route, hour)                     -> speed km/h               40,211 cells
(route)                           -> speed km/h                2,245 rows
(hour)                            -> city speed km/h               24 rows
(route, hour)                     -> services -> wait         41,844 cells
```

Route coverage differs by layer: the served model carries traversal-backed
route speeds for **2,245 routes**; the ping-pair speed layer profiles
**2,303** — 58 routes exist only as ping-pair speeds (no traversal-backed
route-hour speed, so they ride on coarser fallbacks). The edge-cell table
spans 2,196 of the served routes.

Cells are trained with a **minimum of 5 observations**; sparse cells
(5 ≤ n < 24) are **shrunk toward the time implied by their route's
hour-of-day speed** with a pseudo-count of 10, and their p10-p90 widths
scale by n/(n+10). A cell cannot be an extreme median from a handful of
noisy traversals. (Model version `v1.1-quantiles-shrunk`.)

Dwell (stop waiting) is included in passage-to-passage time spans — that is
what a rider experiences — and the data decomposition allows reporting it
separately. The ping-pair layer (which operates without GTFS) applies a
3 km/h floor to filter depot/creep contamination and a 90 km/h ceiling
for GPS noise.

Waiting times are extracted from the feed's own trip labels: distinct
`route_HH_MM` labels per hour, averaged across days, equals services/hour.
Headway = 3600/n, wait = headway/2, clamped to [0.75, 15] minutes.

## 6. Combining live and historical

**Current state (honest):** the live layer is a *switch*, not a blend. When a
route has ≥3 ping pairs in its rolling window, the window speed **replaces**
the historical speed for every leg of that route (a route seen with 3 pairs
in 15 minutes is timed on those pairs alone); otherwise the historical table
is used. Sources:
- **live**: GTFS-RT poller (10s cadence), 45-min store, 15-min estimate window
- **replay**: `?asOf=` with a date (day) or timestamp (45-min window) — a
  parallel implementation with ≥4 pairs, same formula

The documented weighted blend (`alpha * recent + (1-alpha) * historical`,
pair-count-weighted clamped ratio applied on the edge medians) is the design
target in the backlog (B3) — implemented and live-tested results will replace
this paragraph when it lands. The edge Worker serves the pooled model only
(no live arm yet: A6).

## 7. Validation results

> **Correction (2026-11-16):** the first deployed model's hour bins were
> misaligned by 5.5 hours. `hour(to_timestamp(ts + 19800))` applies the IST
> offset a second time whenever the DuckDB session timezone is IST — which
> every locally-run training pass was. The deployed Worker actually serves
> with UTC-session semantics, so its bins were correct; the artifacts built
> locally were shifted, flattening the by-hour signal. All binning is now
> pure integer arithmetic (`((ts+19800)//3600)%24`), regression-tested under
> three session timezones (`tests/test_time_bins.py`), and the full-month
> retrain ran on UTC infrastructure with the fix verified live: the served
> city curve runs 12.7 km/h at the 18:00 IST trough against 25.4 km/h at
> 03:00 IST.

### 7.1 Speed model (route x hour) — ping-pair layer

These numbers score the **ping-pair speed layer** with **31
leave-one-day-out folds** — **not** the served edge model. The served
traversal arm (the 853,480 route-edge-hour cells behind ride times) is not
yet LODO-scored; its accuracy is exercised only through the journey holdout
in §7.2.

| Metric | Value |
|---|---|
| Days cleaned | **31 (day 16 excluded from edge cells; its pings feed speeds)** |
| Pings kept | **560.6M of 912.9M (61.4%)** |
| Cell-day evaluations | 801,551 |
| **Model MAE** | **1.96 km/h** |
| City-hour baseline | 3.07 km/h |
| **Lift** | **36.1%** |

A day-of-week variant was retested on the full 31 days, as the harness
prescribes: the dow arm degraded MAE by 9.8% against the pooled model
(weekends are still too thin to model separately), so route-hour pooling
remains the served model arm. The retest, the rejection, and the arm's
permanently-off switch (`USE_DOW = False`) are decided by the same
leave-one-day-out harness that produces the numbers above.

### 7.2 Journey holdout (real multi-hop rides, strict time split)

> **Pending (2026-11-16):** the journey-level holdout has been rebuilt
> (backlog F1/F2) to chain one vehicle trip's traversals into real
> **5-40-hop rides** and score the whole ride, with a strict time split —
> model trained on **Oct 1-24**, scored on **Oct 25-31**, predictions from
> train-window statistics only. The rerun against the full-month model is
> still in flight; final numbers will be quoted from
> `model/holdout_journeys.json` when it lands, reported beside the mean
> ride time so the MAE has scale.
>
> The previously published replay table (day-13 "365 samples", days 28-30
> "72 samples", 1.22-1.66 min MAE, 98-100% within 10 min) is **withdrawn**:
> it scored single stop-to-next-stop hops on days that sat in the training
> list, so those figures were neither journey-level nor held out. Coverage
> caveat to carry into the replacement numbers: the old harness sampled 72
> replays and could score only **36** ("n=36 scored") — small-sample, so
> the new headline claims will state an explicit n and the fallback-leg
> share.

### 7.3 Google Maps comparison (20 Delhi landmark pairs)

Live comparison against the **Google Routes API v2** (`computeRoutes`,
travelMode `DRIVE`, routingPreference `TRAFFIC_AWARE`) at the exact same
departure instant our planner is queried at — Wednesday 2026-11-18, 09:00 IST
and 15:00 IST, two neutral comparison slots (deliberately not labelled
"rush/quiet": Google's own drive times run slower at 15:00 than at 09:00 on
15 of these 20 pairs). All values are real API responses collected by
`validation/gmaps_routes.py`; detailed table: `docs/od_comparison.json`.
Our side is the live Cloudflare Worker (`/api/journeys`), not a local copy.

| OD pair | Our bus (min) | Google car + traffic (min) | Ratio |
|---|---|---|---|
| Kashmere Gate ISBT → Nehru Place | 54.6 | 46.5 | 1.17× |
| Anand Vihar ISBT → Dhaula Kuan | 66.6 | 70.2 | 0.95× |
| AIIMS → Rohini West | 100.9 | 51.2 | 1.97× |
| IIT Delhi → India Gate | 47.1 | 21.1 | 2.23× |
| Sarojini Nagar → IHC | 33.4 | 16.8 | 1.99× |
| Kalkaji Mandir → Karol Bagh | 95.5 | 53.1 | 1.80× |

Across all 20 pairs the bus:car ratio at the 09:00 comparison slot is
**0.95×–2.23× (median 1.62×)** — physically correct: buses share road space
with cars and
add dwell (stops) and signal overhead, which compounds on cross-town
journeys. Note our planner *beats* the car on Anand Vihar → Dhaula Kuan
because the bus takes the direct Ring Road pattern while Google's car route
detours (26.7 km for a 16 km crow-fly). 20/20 pairs answered by the deployed
planner (9 of them require a transfer, which the edge worker rebuilds via
its 1-transfer search). Re-measured 2026-11-16 against the full-month model
after the hour-bin fix; median moved from 1.60× to 1.62× because the served
route speeds barely changed — the local artifacts carried the bug, the
sensible deployed curve did not. All values reproducible via
`validation/gmaps_routes.py --reuse-google`.

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
* **Key security.** Two Google keys, both stored as Cloudflare Worker
  *secrets*. The server key (`GOOGLE_MAPS_KEY`, restricted to Places and
  Routes) is only used inside the Worker and the browser never sees it; all
  server-side Google calls are proxied through three documented endpoints:
  `/api/places/autocomplete`, `/api/places/resolve`, `/api/gmaps-drive`.
  The Maps JavaScript API cannot run without a key in the page, so the
  basemap uses a separate browser key (`GOOGLE_MAPS_BROWSER_KEY`) limited to
  the Maps JavaScript API and this site's HTTP referrer, served only to
  same-origin requests by `/api/maps-config`.

Two engineering notes for reproducibility: (1) Google's field masks must be
delivered via the `$fields` **query parameter** through Cloudflare Workers —
the `X-Goog-FieldMask` header is silently dropped by the Worker runtime for
these endpoints, which Google surfaces as "FieldMask is a required
parameter"; (2) the Routes API rejects `departureTime` values in the past
(even seconds stale), so the proxy clamps out-of-window departures and lets
Google fall back to live traffic.

## 8. Limitations

- Walk legs are straight-line x1.33 (OSRM upgrade pending)
- ETA p10/p90 add per-leg quantiles — that assumes perfectly correlated
  leg times (a spread upper bound, not an independence approximation);
  band coverage is being measured on held-out journeys (B7)
- Day-of-week keying disabled on the evidence, not on data volume: the
  31-day retest still costs +9.8% MAE vs the pooled model (weekends too
  thin to model separately); `USE_DOW = False` stays
- Day 16 excluded from stop-level model (pathological archive member);
  its GPS data still enters the speed/aggregate layers
- 7z global SHA256 does not verify on our copy (concurrent resume
  corruption; individual members validated at extraction)

## 9. Deployment

- **Edge (laptop-free):** Cloudflare Workers + D1 in APAC
  `https://delhi-pulse.prayaas.workers.dev` — real route shapes, speeds,
  headways, City Pulse map
- **Full planner (tunneled):** FastAPI on the machine, Cloudflare Tunnel
  for public URL — full journey planner with transfers, replay, live layer
  (a switch, not a blend — §6)
- **Reproducibility:** `pip install -r requirements.txt` + full pipeline runs
  from scratch; synthetic fixture for end-to-end testing without downloads
- **Open source:** `github.com/crystalcoderz/delhi-pulse`
