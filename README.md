# Delhi Pulse
### Real-time bus traffic model and journey planner — Transport Stack Hackathon 2026, Problem 1

Buses-as-probes traffic model for Delhi: turn one month of DTC bus GPS into
time-dependent travel times, and serve them through a Google Maps-style
journey planner API.

## Quickstart (synthetic data — no downloads needed)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe pipeline\synth_fixture.py
.venv\Scripts\python.exe pipeline\02_clean_pings.py
.venv\Scripts\python.exe pipeline\03_project_trips.py
.venv\Scripts\python.exe pipeline\04_train_model.py
.venv\Scripts\python.exe -m uvicorn api.app:app --port 8000
# open http://localhost:8000/web  (or web/index.html with API_URL override)
```

## Real data flow

```
delhi-dtc-bus-gtfs-rt-2026-10.7z  ──► 01_extract_day.py   (one day at a time)
    bus_movements_YYYY_MM_DD.db   ──► 02_clean_pings.py  (dedupe, bbox, jump filter)
                                   ──► 03_project_trips.py (ping→shape projection,
                                        stop passages, edge traversals)
                                   ──► 04_train_model.py  (medians + fallbacks)
model/*.parquet + data/gtfs/*.txt ──► api/app.py  (/journeys, /stops/search, /pulse)
                                   ──► web/index.html (MapLibre UI)
```

## Layout

```
pipeline/   01 extract · 02 clean · 03 project · 04 model · synth_fixture (dress rehearsal)
api/        FastAPI journey planner
web/        single-file MapLibre client
model/      trained lookup tables (parquet)
data/       raw + processed (gitignored)
validation/ hold-out harness (D12)
deployment/ vm_setup.sh — one-shot CoreWeave bootstrap
```

## VM run (CoreWeave)

```bash
bash deployment/vm_setup.sh            # installs tools, downloads data, runs pipeline
# or step-by-step: see deployment/vm_setup.sh
```
