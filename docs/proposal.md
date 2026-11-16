# Delhi Pulse — Phase 1 Proposal

## Overview

Delhi Pulse is a real-time bus traffic model and journey planner built for the Transport Stack Hackathon 2026, Problem 1. It is trained on the actual GPS exhaust of Delhi's DTC fleet and answers one question honestly: how long will this bus actually take? Instead of trusting timetables, we learned travel-time distributions from observed vehicle movement and serve them through a planner that always finds options. Phase 1 is the complete loop: ingest, match, model, validate, deploy.

## What We Built

- **Full pipeline, open source end to end**, from raw GPS pings to a deployable API.
- **Quantile ETAs.** The core data object is 839,382 (route, edge, hour) cells, each holding p10/p50/p90 travel times learned from real observations, covering 2,215 Delhi bus routes and 6,862 real stops.
- **Journey planner API.** `GET /journeys?from=&to=&depart=` returns walk, wait, bus ride, and transfer legs, each with quantile ETA bands instead of one misleading number.
- **Headway-aware waiting.** Wait times come from observed bus frequency per (route, hour), extracted from trip labels, never from guessed timetables.
- **Time-travel replay.** `?asOf=date` re-plans any journey using that October day's observed speeds.
- **Live GTFS-RT blend.** 5,990 vehicles tracked, with journeys answered in live mode against the current network.
- **City Pulse map.** 2,574 route shapes colored by real observed speeds, showing where Delhi actually moves.

## Data & Validation

We processed 19 days of October 2026 DTC GPS data (Oct 13–31): ~550 million raw GPS observations from ~5,000 buses, cleaned to 330 million records. 669,362 bus trips were matched to real route shapes, a 92% match rate.

Validation is leave-one-day-out and honest: each day is held out and predicted from the other eighteen.

- Speed model MAE: **1.92 km/h**, 37% better than a city-average baseline, evaluated over 502,158 cells across 19 folds.
- Journey replay MAE: **1.22–1.66 minutes** on the real network.
- **93.3%** of speed estimates fall within 5 km/h.
- **0% no-answer rate** — the planner always finds options.

## Technical Discoveries

Two findings shaped the model. First, the archive's `trip_id` is a runtime label (`route_HH_MM`), not a GTFS trip_id, so we built a route-keyed best-shape-fit matcher to recover true trip geometry. Second, we caught depot-creep contamination: parked buses with GPS running were poisoning ping-pair speeds, deflating morning medians to 18 km/h; after filtering, real morning speeds are 26 km/h. We also tested day-of-week keying and rejected it — the honest harness shows 19 days cannot support that split.

## Deployment & Links

Everything runs on Cloudflare Workers in the APAC region, independent of anyone's laptop, so the live API is always on. Every number above is measured, not promised, and is already served at the live endpoint.

- GitHub: github.com/crystalcoderz/delhi-pulse
- Hugging Face: huggingface.co/datasets/PRAYAASOG/delhi-pulse-model
- Live edge: delhi-pulse.prayaas.workers.dev
