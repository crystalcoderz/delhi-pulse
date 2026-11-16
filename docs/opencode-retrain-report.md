# Delhi Pulse: retrain and bug-fix report for opencode

Written 16 November 2026 for the retraining run. Repo: `crystalcoderz/delhi-pulse`, branch `v0/ui-redesign` (base `main`), HEAD `9390b9d`. Live Worker: `https://delhi-pulse.prayaas.workers.dev`.

The full defect list with file and line references is `docs/backlog.md`. This report says what to change first, how to retrain, and how to prove the retrain worked. Line numbers in the backlog were taken at commit `96127f4`; `workers/worker.js` changed in `9390b9d`, so search for the code rather than trusting line numbers there.

## 1. What the project is

Bus travel-time model and journey planner for Delhi (Transport Stack Hackathon 2026, Problem 1).

- Input: about 550M GPS pings from about 5,000 DTC buses, October 2026, plus static GTFS.
- Pipeline (`pipeline/`): 01 extract a day, 02 clean, 03 project pings onto GTFS shapes, 04 train edge-by-hour quantiles, 04f ping-pair speeds, 04g headways, 05 network speed model.
- Serving: FastAPI locally (`api/app.py`, `api/live.py`), and a Cloudflare Worker with D1 and static UI in production (`workers/worker.js`, `workers/public`). The Worker reads `model.json`; it does not run Python.
- Export to the Worker: `deployment/export_model_json.py` and the D1 export scripts in `deployment/`.

## 2. State right now

Done and live:
- Google key exposure is fixed. `/api/maps-config` serves only a referrer-restricted browser key, same origin only. The server key (Places and Routes) is never sent to a browser. Both are Worker secrets. Git history and tracked files contain no key. Backlog item E1 is closed.

Not done (everything else in `docs/backlog.md`). The ones that change the model, and so need a retrain, are:

| ID | Problem | Why it needs a retrain |
|---|---|---|
| A1 | Hour bins are 5.5 hours ahead of IST (`pipeline/02_clean_pings.py`, `pipeline/04f_pings_speeds.py`) | Every hour-keyed speed, headway and edge cell is assigned to the wrong hour |
| B1 | Deployed speeds drop ping pairs under 3 km/h or under 15 m, so time stopped at stops is missing | Rush-hour effect is understated |
| B8 | Edge model trained on 18 of 31 days | Use the full month |
| B6 | Quantiles kept from as few as 3 samples; `meta.json` says "p50 only" | Sparse cells are noisy; shrink toward the route-hour value |
| B9 | Leg length is straight-line between stops | Export the along-route distance stage 03 already measures |

## 3. Order of work

Do these in order. Each step builds on the one before.

1. **Fix the hour bins (A1).** Replace `hour(to_timestamp(ts_utc + 19800))` with `((ts_utc + 19800) // 3600) % 24`, and compute day of week with the same arithmetic. Apply it in `pipeline/02_clean_pings.py` and `pipeline/04f_pings_speeds.py`, and grep the repo for any other use of `to_timestamp(` or `hour(` on timestamps. Add a unit test: a ping at `2026-10-13 03:00:00Z` must land in hour 8 (08:30 IST).
2. **Rerun stages 02, 03 (only if it reads hours), 04, 04f, 04g and 05** for all 31 days (`deployment/vm_bootstrap.sh` runs the full month). Rerun `04g` as well, since headways are also keyed by hour.
3. **Train travel times that include stopped time (B1).** Export edge-by-hour cells from stage 04 (the traversal times, which include dwell) and use `route_hour.parquet` as the fallback. Stop reading the moving-only table in `deployment/export_model_json.py`. Swap the fallback order in `api/app.py` (B2).
4. **Fix quantiles (B6, B7).** Raise the minimum sample count, shrink sparse cells toward the route-hour value, and stop adding per-leg p10 and p90 across legs. Calibrate on held-out data so the p10 to p90 band contains about 80% of actual times.
5. **Ship the model to the Worker (A2, A3, A5).** The Worker must use the edge-by-hour medians and the real p10 and p90, and return the quieter-hour time (`usual_p50_min`, `rush_effect_min`). `/status` must report a non-zero `edges`.
6. **Planner and API bugs that a judge hits first:** D1 (`depart` parsing returns 500 and reads unzoned times as UTC), D2 (direct-journey band excludes the final walk), C1 (repeated options), B4 (buses offered at hours with no service), D3, D4. Details and fixes are in the backlog.
7. **Validation (F1, F2, F5).** Rebuild validation on real multi-stop trips, with training on 1 to 24 October and testing on 25 to 31 October. Report the numbers in section 5.
8. **Update the README and `docs/technical-note.md` (A9)** so they describe what is deployed, including honest numbers.

Skip the speculative items in backlog section H until the above is done.

## 4. How to retrain and deploy

1. Run the stages above for all days; outputs go under `data/` and `model/`, which are gitignored. Do not commit raw or processed data.
2. Export with the scripts in `deployment/` to produce `model.json` and the D1 SQL.
3. Deploy from `workers/`: load D1 with `npx wrangler d1 execute delhi-pulse --remote --file <sql>`, then `npx wrangler deploy`.
4. D1 free plan allows 100,000 row writes per day. The export is capped at an 80,000-row budget on purpose (commit `9d3879d`). Keep it under that.

## 5. Acceptance checks

Do not call the retrain done until all of these hold. Report the actual numbers, including bad ones.

- Hour test passes (a ping at 03:00 UTC is hour 8).
- Held-out, not in-sample: train on 1 to 24 October, score on 25 to 31 October. The current "journey replay" MAE of 1.22 to 1.66 min scores single stop-to-next-stop hops on days that were also in training, so it cannot be compared with a new figure.
- Score real journeys: chain one trip's traversals between stops 5 to 40 apart, and score ride time. Report the mean ride time next to the MAE so the error has a scale.
- Report error by hour band and by trip length, and the share of actual times inside the p10 to p90 band (target about 80%).
- Add a timetable baseline and an observed bus time for the 20 comparison pairs (median of archive trips serving both stops in that hour), beside the model and Google's driving time.
- Rush hour is slower than off-peak for the same route in the model's output. This was not true before: the quiet 15:00 hour was slower than 09:00 for Google in 15 of 20 pairs, and the deployed speeds were nearly flat by hour.
- Live checks on the Worker after deploy:
  - `/status` shows `edges` greater than 0.
  - A 03:00 query does not offer a bus with a 5-minute wait unless that route runs at that hour.
  - `depart=2026-11-17T08:30:00+05:30` with an unencoded `+` returns a valid answer, not 500, and an unparseable value returns 400.
  - Kashmere Gate to Nehru Place with `max=6` returns different routes, not one itinerary three times.

## 6. Rules for opencode

- **Never print, log or commit a key.** Names only: `GOOGLE_MAPS_KEY` (server, Places and Routes), `GOOGLE_MAPS_BROWSER_KEY` (browser, Maps JS, referrer-restricted), `GTFS_RT_API_KEY`. Keep them as Worker secrets or in an ignored `.env`. If you need to check one, print its length or a match count only.
- Do not push to `main`. Work on a branch and open a PR.
- Do not change the Worker's public API shape (`/journeys`, `/stops/search`, `/stops/nearby`, `/places/*`, `/gmaps-drive`, `/geo`, `/pulse`, `/status`, `/health`). The UI in `workers/public` depends on it. Behaviour fixes are fine; renamed or removed fields are not. The frozen contract is in `docs/v0-brief.md`.
- Keep the Python planner and the Worker planner in agreement. They have drifted (A4, A5, A7, C5). Add a small set of fixed queries that run against both.
- When a fix changes a number the README or technical note quotes, update the document in the same commit.
- Tick the box in `docs/backlog.md` for each item you finish.

## 7. Still manual, outside the repo (owner action)

- Rotate the Google server key. It was passed through a chat session when the secrets were set.
- Delete the old unrestricted Google key (`c7bcc0b4-7896-4878-b792-f32722cea91d`). The old Worker served it publicly.
- Set a daily quota cap and a billing alert in Google Cloud.
- Add a Cloudflare rate limit to `/api/places/*` and `/api/gmaps-drive`, and restrict their CORS to the site's own origins (E2). This is a code and dashboard change that is not done yet.
