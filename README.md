# Delhi Pulse

**Real-time bus traffic model and journey planner for Delhi** — Transport Stack Hackathon 2026, Problem 1.

> **Live:** [Full journey planner](https://delhi-pulse.prayaas.workers.dev/) — edge-deployed on Cloudflare Workers (APAC), no laptop required
> **Open source:** [github.com/crystalcoderz/delhi-pulse](https://github.com/crystalcoderz/delhi-pulse)
> **Trained model + data:** [huggingface.co/datasets/PRAYAASOG/delhi-pulse-model](https://huggingface.co/datasets/PRAYAASOG/delhi-pulse-model)

---

## What it does

Every DTC bus is a traffic sensor. We process **31 days of October GPS data** — **912.9M raw pings, 560.6M kept after cleaning (61.4%)** — into a quantile travel-time model covering **2,303 real Delhi routes, 6,862 real stops, 853,480 (route, edge, hour) quantile cells trained on 39.2M edge traversals from 1.07M observed bus trips**. On top of that: a journey planner API with walk + wait + ride legs, transfers, headway-aware waiting, time-travel replay, and a live GTFS-RT blend layer.

**The result:** a user asks "Kashmere Gate ISBT to Nehru Place, 8:30am Tuesday" and gets three sensible options with real route numbers, boarding points, walk minutes, quantile ETA bands (p10/p50/p90), and the "usual" time at a quieter hour.

## Architecture

```
Stage 01    Extract single day from 7z archive         (py7zr)
Stage 02    Clean: dedupe, bbox, teleports             (DuckDB sqlite-scanner, ~26s/day)
Stage 03    Project pings onto real GTFS shapes         (8-worker parallel, best-shape fit)
Stage 04    Train: quantile aggregates per edge/hour   (853k cells from 30 days, n>=5 floor + shrinkage)
Stage 04f   Ping-pair speeds (no GTFS needed)           (40k route x hour cells)
Stage 04g   Headways from trip labels                  (42k route x hour cells)
Stage 05    Network-wide aggregate speed model

api/app.py     FastAPI journey planner
api/live.py    GTFS-RT live layer (rolling window + blend)
web/           map clients (planner + City Pulse map)
workers/       Cloudflare Worker + D1 edge deployment
deployment/    VM setup, D1 export, sandbox runner
validation/    Holdout harnesses + OD comparison table
```

## What's measured (honest, leave-one-day-out)

| Test | Metric |
|---|---|
| Speed model (801,551 cells, 31 folds) | **MAE 1.96 km/h** vs city baseline 3.07 (36.1% lift) |
| Journey replay (real bus routes) | **MAE 1.22-1.66 min**, 98-100% within 10 min |
| Planner no-option rate | **0%** — always answers |
| Shape match rate | **92%** of real trajectory segments |
| Route match to GTFS | **99.4%** (2,111 of 2,124 GPS routes found in GTFS) |

## Key findings

1. **`trip_id` in the GPS archive is a runtime label** (`route_HH_MM`), not a GTFS trip_id — stage 03 auto-detects and handles both worlds.
2. **Depot-creep contamination** (sub-3 km/h parked-bus pairs) was discovered and filtered — morning median speeds corrected from ~18 to ~26 km/h.
3. **Ping-pair speeds ≠ journey times**: curb-to-curb movement is nearly flat across hours, but journey-level rush penalty accrues through dwell + signal compounding that only stop-passage data captures.
4. **Day-of-week keying retested at the full month**: the dow arm still degrades MAE by 9.8% vs the pooled model (weekends stay too thin); the honest harness keeps it disabled.
5. **Hour bins were timezone-dependent** (`hour(to_timestamp(ts+19800))` double-shifts on IST sessions) — caught, fixed with integer arithmetic, and regression-tested under three session timezones (`tests/test_time_bins.py`); the full-month retrain carries corrected bins.

## Quick start

```powershell
# 1. install dependencies
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. get the data (see pipeline/sandbox_run.py for the sandbox pipeline,
#    or download the 7z from the hackathon page)
#    .env needs: GTFS_RT_URL + GTFS_RT_API_KEY (from Transport Stack)

# 3. put static GTFS .txt files in data/gtfs/

# 4. process a day
.venv\Scripts\python pipeline\01_extract_day.py --day 2026_10_13
.venv\Scripts\python pipeline\02_clean_pings.py  --day 2026_10_13
.venv\Scripts\python pipeline\03_project_trips.py --day 2026_10_13
.venv\Scripts\python pipeline\04_train_model.py

# 5. serve
.venv\Scripts\python -m uvicorn api.app:app --port 8010
# open http://localhost:8010/web/
```

## API

Deployed edge API: **https://delhi-pulse.prayaas.workers.dev** (same routes
with or without the `/api` prefix).

```
GET /journeys?from=<lat,lng>&to=<lat,lng>&depart=<ISO8601>
    → journey options with walk/wait/ride legs + p10/p50/p90 ETA bands
    (1-transfer search included; stop:<id> also accepted for from/to)
    [&asOf=2026-10-13]   time-travel replay (whole-day)
    [&max=6]              max options returned

GET /stops/search?q=<text>          fuzzy real-stop search
GET /stops/nearby?lat=&lng=         stops within radius with walk minutes
GET /places/autocomplete?input=     Google Places (New) typing suggestions,
                                    Delhi-NCR restricted, server key never sent to the browser
GET /places/resolve?place_id=      place id → name + coordinates
GET /gmaps-drive?from=&to=&depart=  Google Routes v2 traffic-aware driving
                                    time at the same departure (comparison)
GET /geo?hour=9                     route geometries colored by observed speed
GET /pulse                          real city speed curve
GET /days                           available replay days (local API)
GET /status                         system + live-layer + holdout readout
GET /health                         lightweight health
```

The planner UI searches **both** DTC stops and any Delhi landmark via Google
Places, and shows a live "Google Maps: X min by car with traffic" comparison
beside every result. The Google key is a Cloudflare Worker secret and never
reaches the browser.

## Edge (Cloudflare Workers + D1)

```powershell
# deploy the edge slice (City Pulse + lookups)
cd workers
npx wrangler d1 execute delhi-pulse --remote --file ../docs/d1_part1.sql
npx wrangler deploy
# live at https://delhi-pulse.prayaas.workers.dev/
```

### Vercel (UI previews)

Vercel deploys only the static UI in `workers/public`; `vercel.json` proxies
the API paths to the Cloudflare Worker above, so the client's same-origin
calls work unchanged. `.vercelignore` keeps the Python pipeline and
`api/` out of the build (it is not a Vercel function and its dependencies
exceed the 500 MB function limit). A Worker route added on a branch is only
reachable from a Vercel preview after `npx wrangler deploy`.

## Project structure

```
pipeline/          production pipeline stages (01-05 + synth + runner)
api/               FastAPI planner + live GTFS-RT layer
web/               map clients (planner + City Pulse map)
workers/           Cloudflare Worker + D1 edge deployment
validation/        holdout harnesses + OD comparison
deployment/        VM setup, D1 export, sandbox runner
dev/               one-off scripts (probes, QA, forensics)
docs/              technical note, report.json, OD comparison
data/              raw + processed (gitignored, ~5 GB local)
model/             trained lookup tables (gitignored parquet)
```

## Tech stack

Python 3.11 · DuckDB · py7zr · FastAPI · NumPy · httpx · Google Maps JavaScript API
Cloudflare Workers + D1 · cwsandbox (CoreWeave/W&B)
