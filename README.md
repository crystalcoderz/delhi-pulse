# Delhi Pulse
### Real-time bus traffic model and journey planner — Transport Stack Hackathon 2026, Problem 1

Buses-as-probes traffic model for Delhi: one month of DTC bus GPS becomes
time-dependent travel times, served through a Google Maps-style journey
planner API with transfers, headway-based waits, quantile ETA bands,
and day/window time-travel replay.

## Quickstart (synthetic dress rehearsal, no downloads)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe pipeline\synth_fixture.py
.venv\Scripts\python.exe pipeline\02_clean_pings.py
.venv\Scripts\python.exe pipeline\03_project_trips.py
.venv\Scripts\python.exe pipeline\04_train_model.py
.venv\Scripts\python.exe -m uvicorn api.app:app --port 8010
# UI:   http://localhost:8010/web/     city view: /web/pulse.html
```

## Real-data flow (W&B/CoreWeave sandboxes or any Linux box)

```powershell
# one real day end-to-end inside a sandbox (downloads 16 GB from R2 itself):
.venv\Scripts\python.exe pipeline\sandbox_run.py --days 2026_10_13
# more days (attach reuses the warm sandbox + archive):
.venv\Scripts\python.exe pipeline\sandbox_run.py --days 2026_10_17 2026_10_18 --attach --skip-download
# GTFS-free route x hour speed profiles per processed day:
.venv\Scripts\python.exe pipeline\04f_pings_speeds.py --all
# network-wide real speed model from all processed days:
.venv\Scripts\python.exe pipeline\05_ping_model.py
# validation (edge replay; temporal hold-out via --test-days):
.venv\Scripts\python.exe validation\holdout.py --n 2000
```

Per-day outputs land in `data/clean/` (pings parquet ~240 MB/day,
speeds, stats json). Model tables in `model/`.

## Pipeline stages

```
01_extract_day.py    single-day member from the 7z (py7zr), SQLite indexes
02_clean_pings.py    DuckDB-over-SQLite direct scan: dedupe, bbox, teleports,
                     IST hour/dow  +  drop-reason stats json (real: 62% kept)
03_project_trips.py  pings -> GTFS shapes; dual mode: trips (synth) /
                     routes (real: trip_id is a runtime label like 1795_19_31);
                     gap segmentation, best-shape fit, stop passages, edges
04_train_model.py    edge p10/p50/p90 + headways + fallback hierarchy
04f_pings_speeds.py  route x hour speeds from ping pairs (no GTFS needed)
05_ping_model.py     network-wide real speed tables across all days
```

## API

```
GET /health   GET /status            system + live-layer + holdout readout
GET /stops/search?q=                  fuzzy stop search
GET /journeys?from=&to=&depart=       options: walk/wait/bus legs, \
     [&asOf=2026-10-13 | 2026-10-13T09:30:00+05:30]      day / window replay
GET /geo?hour=9[&day=]                route polylines colored by observed speed
GET /days  GET /pulse                replay day list; city speed curve
```

`asOf` is the time-travel feature: date-only re-plans with that day's
observed speeds; a full timestamp re-plans using just the prior 45 minutes
of bus movement — the same blend path the live GTFS-RT feed will use
(`api/live.py`, starts automatically when `GTFS_RT_URL` is in the env).

## Layout

```
pipeline/   stages 01-05 + synth fixture + sandbox runner + QA scripts
api/        FastAPI planner (app.py), live layer (live.py)
web/        single-file MapLibre clients (index.html planner, pulse.html city)
model/      trained lookup tables (parquet) + holdout reports
data/       raw + processed (gitignored)
validation/ holdout harness
deployment/ vm/sandbox setup, R2 sync, Cloudflare wiring
```

## Real-data notes (see docs/technical-note.md for the full write-up)

- Day 13 cleaning: 28.7M rows -> 17.8M kept (62.0%), ~26 s after the
  DuckDB sqlite-scanner rewrite.
- `trip_id` in the archive is a runtime label (`route_HH_MM`), NOT a GTFS
  trip id — stage 03 auto-detects and handles both worlds.
- 2,112+ real routes profiled per day from ping pairs; network-wide real
  model currently serves /geo, /pulse and replay.
