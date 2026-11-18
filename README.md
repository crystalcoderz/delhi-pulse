# Delhi Pulse

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Edge API](https://img.shields.io/badge/live-edge%20API-0b5fff)](https://delhi-pulse.prayaas.workers.dev)
[![Demo Video](https://img.shields.io/badge/watch-demo%20video-ff0000)](https://github.com/crystalcoderz/delhi-pulse/releases/tag/demo-video)

**Real-time bus traffic model and journey planner for Delhi** — Transport Stack Hackathon 2026, Problem 1.

> **Live:** [Full journey planner](https://delhi-pulse.prayaas.workers.dev/) — edge-deployed on Cloudflare Workers (APAC), no laptop required
> **Watch:** [2-minute demo video](https://github.com/crystalcoderz/delhi-pulse/releases/tag/demo-video) — City Pulse, journey planner, API playground, live GTFS-RT
> **Open source:** [github.com/crystalcoderz/delhi-pulse](https://github.com/crystalcoderz/delhi-pulse)
> **Trained model + data:** [huggingface.co/datasets/PRAYAASOG/delhi-pulse-model](https://huggingface.co/datasets/PRAYAASOG/delhi-pulse-model)

---

## What it does

Every DTC bus is a traffic sensor. We process **31 days of October GPS data** — **912.9M raw pings, 560.6M kept after cleaning (61.4%)** — into a quantile travel-time model covering **2,245 real Delhi routes in the served edge model (2,303 profiled in the speed layer), 6,862 real stops, and 853,480 (route, edge, hour) quantile cells trained on 39.2M edge traversals from 1.07M observed bus trips**. On top of that: a journey planner API with walk + wait + ride legs, transfers, headway-aware waiting, and a live GTFS-RT blend layer in the local API (the edge Worker serves the pooled full-month model — see the technical note for the exact arm split).

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
| Speed layer (ping-pairs; 801,551 cell-day evals, 31 leave-one-day-out folds) | **MAE 1.96 km/h** vs city baseline 3.07 (36.1% lift) |
| Journey holdout (5-40 hop rides, train Oct 1-24, score Oct 25-31) | **MAE 4.6 min** on mean ride 30.9 min (14.9% of ride time), n=4,178, 90.5% within 10 min, band coverage 84.5% |
| Planner dead hours | returns `options: []` with a note (no observed service = no bus; verified 02:00-04:00) |
| Shape match rate | 92% of real trajectory segments |
| Definitional note | `trip_id` values are runtime `route_HH_MM` labels; "observed trips" counts label-days, not physical buses |

## Key findings

1. **`trip_id` in the GPS archive is a runtime label** (`route_HH_MM`), not a GTFS trip_id — stage 03 auto-detects and handles both worlds.
2. **Depot-creep contamination** (sub-3 km/h parked-bus pairs) was discovered and filtered — morning median speeds corrected from ~18 to ~26 km/h.
3. **Ping-pair speeds ≠ journey times**: curb-to-curb movement is nearly flat across hours, but journey-level rush penalty accrues through dwell + signal compounding that only stop-passage data captures.
4. **Day-of-week keying retested at the full month**: the dow arm still degrades MAE by 9.8% vs the pooled model (weekends stay too thin); the honest harness keeps it disabled.
5. **Hour bins were timezone-dependent** (`hour(to_timestamp(ts+19800))` double-shifts on IST sessions) — caught, fixed with integer arithmetic, and regression-tested under three session timezones (`tests/test_time_bins.py`); the full-month retrain carries corrected bins.

## Quick start

```bash
# 1. dependencies (pinned in requirements.txt; `make setup` creates the venv)
python -m venv .venv
#   Windows:        .venv\Scripts\activate
#   macOS / Linux:  source .venv/bin/activate
python -m pip install -r requirements.txt
# make targets assume that venv is active, or pass PY=.venv/Scripts/python.exe

# 2. data: 15 GB October archive -> data/ (URLs live in pipeline/common.py)
make fetch-archive            # downloads + verifies SHA256
# no make? download DATA_URL by hand into data/ and check it against SUMS_URL
#    .env needs: GTFS_RT_URL + GTFS_RT_API_KEY (from Transport Stack)

# 3. static GTFS .txt files -> data/gtfs/

# 4. process a day
python pipeline/01_extract_day.py --day 2026_10_13
python pipeline/02_clean_pings.py  --day 2026_10_13
python pipeline/03_project_trips.py --day 2026_10_13
python pipeline/04_train_model.py

# 5. serve
python -m uvicorn api.app:app --port 8010
# open http://localhost:8010/web/
```

> **Skip the 15 GB pipeline:** `make model` downloads the trained
> `model/*.parquet` tables and `model/*.json` meta from the
> [HF dataset](https://huggingface.co/datasets/PRAYAASOG/delhi-pulse-model)
> straight into `model/`. With the static GTFS `.txt` files from step 3 that is
> enough for `make serve` and for the edge export — no archive, no per-day
> stages. `make model-train` rebuilds the tables locally over every processed day.

`make` is optional: every target in the `Makefile` (`setup`, `fetch-archive`,
`model`, `model-train`, `train`, `train-month`, `serve`, `deploy`, `check`) is
a thin wrapper over the `python -m` commands above, and each step can be run by
hand exactly as shown (a manual archive download just has to land in `data/` —
use the `DATA_URL` from `pipeline/common.py`).

## API

Deployed edge API: **https://delhi-pulse.prayaas.workers.dev** (same routes
with or without the `/api` prefix).

Try every endpoint in the browser at
**https://delhi-pulse.prayaas.workers.dev/playground**: edit the parameters,
send the request, and read the response as a JSON tree or as a drawn preview
(journey legs, route speeds, the city speed curve). Each request has a
shareable link, and the planner links to the exact request behind its results.

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
                                    time at the same departure (comparison; endpoints limited to the north-India guard box, ≤120 km apart)
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

```bash
# 0. regenerate the D1 SQL from model/ (gitignored build output; the exporter
#    writes docs/d1_export.sql and splits it into docs/d1_partN.sql itself):
python deployment/export_d1.py

# deploy the edge slice (City Pulse + lookups)
# D1 import is THREE ordered parts today -- each imports cleanly on its own,
# and importing only part1 leaves the edge model incomplete. If the exporter
# prints more parts, import those too.
cd workers
npx wrangler d1 execute delhi-pulse --remote --file ../docs/d1_part1.sql
npx wrangler d1 execute delhi-pulse --remote --file ../docs/d1_part2.sql
npx wrangler d1 execute delhi-pulse --remote --file ../docs/d1_part3.sql
npx wrangler deploy
# live at https://delhi-pulse.prayaas.workers.dev/
# (`make deploy` runs the export, the three imports and deploy from the repo root)
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
model/             trained lookup tables (gitignored; `make model` pulls them from HF)
```

## Tech stack

Python 3.11 (dependencies pinned in `requirements.txt`) · DuckDB · py7zr · FastAPI · NumPy · httpx · Google Maps JavaScript API
Cloudflare Workers + D1 · cwsandbox (CoreWeave/W&B)

## License
**Code:** MIT — see [LICENSE](LICENSE). The license covers the source code in
this repository only, not the input data or the derived model tables.

**Input data (not redistributed here):**
- **DTC bus GPS archive, October 2026** — `delhi-dtc-bus-gtfs-rt-2026-10.7z`
  (16.25 GB, ~913M pings, 31 daily SQLite members). Published by
  **Delhi Transport Corporation (DTC)** and distributed to hackathon
  participants by the **Transport Stack** data platform (Delhi Government)
  under its dataset terms of use. We do not re-host the archive;
  `pipeline/common.py` points at the participant mirror alongside a
  `SHA256SUMS` file.
- **Static GTFS** — supplied via the same **Transport Stack** dataset portal,
  agency `DTC` / *Delhi Transport Corporation* (`agency.txt`). Attribution to
  DTC as the feed's data provider is retained.

**Derived data we do publish** (Hugging Face `PRAYAASOG/delhi-pulse-model`,
`docs/d1_*.sql`): only aggregates — (route, edge, hour) quantiles, route×hour
speeds, headways, route shapes, the city hour curve. No individual vehicle,
trip, plate, or raw ping row is redistributed. Derived aggregates are shared
under the same terms as the input data; contact the maintainers for reuse
beyond the hackathon.
