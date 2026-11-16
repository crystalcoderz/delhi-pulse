# Delhi Pulse improvement backlog

Reviewed on 16 November 2026 at commit `96127f4` (branch `v0/ui-redesign`). Nothing on this list has been fixed yet. Section A is carried over from the first review; sections B to H are new.

How to read it:

- **P1** affects the judged accuracy, a requirement in the brief, or exposes you to cost. **P2** is wrong or fragile behaviour. **P3** is cleanup.
- **Live** means it was seen on the deployed Worker (`delhi-pulse.prayaas.workers.dev`). **Code** means it was read in the source and not run.
- Tick the box when an item is done.

## Suggested order

1. Fix the hour bins and rerun the speed stages (A1).
2. Put travel times that include stopped time on the Worker (A2, B1).
3. Fix what a judge will hit first in the API: `depart` parsing (D1), the band bug (D2), repeated options (C1), buses offered outside service hours (B4).
4. Split the Google key and rate-limit the Google proxies (E1, E2).
5. Rebuild validation on real multi-stop trips with a time split (F1, F2, F5), then update the README and technical note (A9).

## A. Still open from the first review

- [x] **P1 A1. *(done — fixed 2026-11-16: integer arithmetic + tests/test_time_bins.py (3 session timezones); full-month retrain verified)* Hour bins are 5.5 hours ahead of IST.** `hour(to_timestamp(ts_utc + 19800))` in `pipeline/02_clean_pings.py:81` and `pipeline/04f_pings_speeds.py:37` adds the offset twice when the DuckDB session is in IST. Use `((ts_utc + 19800) // 3600) % 24` and the matching day-of-week arithmetic, then rerun 04f, 05 and the export.
- [ ] **P1 A2. The Worker does not use the edge model.** Ride time is straight-line stop distance divided by route speed (`workers/worker.js:419-434`), and `/status` reports `edges: 0`. Ship the edge-by-hour medians.
- [ ] **P1 A3. ETA bands on the Worker are fixed multipliers** (0.85 and 1.35, `workers/worker.js:467-468`). Use the p10 and p90 that stage 04 already computes.
- [x] **P1 A4. *(done — verdict 2026-11-16: dow arm retested at 31 days by the harness, -9.8% vs pooled -> USE_DOW stays False)* Day of week is ignored.** Tuesday 08:30 and Sunday 08:30 return the same answer; `USE_DOW` is off (`api/app.py:181`). See B8.
- [ ] **P1 A5. The Worker never returns the quieter-hour time** (`usual_p50_min`, `rush_effect_min`). The local API does (`api/app.py:551-578`) and the UI already renders it. See C7 before porting.
- [ ] **P1 A6. The Worker has no live data** (`live: {vehicles: 0}`). See B3 and section H.
- [ ] **P2 A7. Stop names match by first substring** (`workers/worker.js:357`), so "Kashmere Gate ISBT" fails. Port the fuzzy match the local API uses.
- [ ] **P1 A8. Validation gaps.** The Google comparison is driving time, none of the 20 pairs has an observed bus time, and the "quiet" hour (15:00) is slower than 09:00 for Google in 15 of 20 pairs.
- [x] **P2 A9. *(done — done 2026-11-16: README + technical note rewritten with deployed full-month numbers + hour-bin honesty note)* The README and technical note describe the local model**, not what is deployed. Update them after the fixes above.

## B. Model accuracy

- [ ] **P1 B1. Deployed speeds leave out time spent stopped.** Stage 04f drops ping pairs slower than 3 km/h or shorter than 15 m (`pipeline/04f_pings_speeds.py:18-20`), and the export reads that table (`deployment/export_model_json.py:82`). README finding 3 says these speeds are nearly flat across hours, so expect the rush-hour effect to stay small even after A1. Export travel times that include dwell: the edge cells, with stage 04's `route_hour.parquet` as the fallback.
- [ ] **P2 B2. The local fallback chain prefers the moving-only speeds** over the traversal speeds (`api/app.py:225-228`, `237-239`). Swap the order.
- [ ] **P1 B3. The live blend is a switch.** The technical note describes `alpha * recent + (1 - alpha) * historical`; no such weighting exists in `api/`. When any live window exists, edge medians are skipped for every route (`api/app.py:219`, `342`), and a route with 3 ping pairs in 15 minutes is timed on those alone (`api/live.py:84`). Implement the blend, weight it by pair count, and apply it as a ratio on the edge medians. (Code.)
- [x] **P1 B4. *(done — fixed 2026-11-16: service-aware waits (headway within ±1h required) in direct + transfer searches)* The planner offers buses at hours with no observed service.** Live: a 03:00 query returns route 425UP with a 5-minute wait. That is the default used when the route has no headway for the hour (`workers/worker.js:435-436`). Treat a missing headway as not running and report the next service.
- [ ] **P2 B5. Wait is half the headway, with no spread.** That assumes evenly spaced buses, and the band treats the wait as exact. Use the observed gaps between buses (mean squared gap divided by twice the mean gap) and include the wait in p10 and p90.
- [x] **P2 B6. *(done — fixed 2026-11-16: n>=5 floor, sparse cells shrunk toward route-hour implied time (K=10); meta v1.1-quantiles-shrunk)* Quantiles are kept from as few as 3 samples** (`pipeline/04_train_model.py:44`). Raise the floor and shrink sparse cells towards the route-hour value. `meta.json` still says "p50 only" (`pipeline/04_train_model.py:134`) although p10 and p90 are computed.
- [ ] **P2 B7. Trip bands add up per-leg p10s and p90s**, which makes them too wide. The note calls this an independence approximation; it is the opposite assumption. Check on held-out trips that the band contains about 80% of actual times.
- [x] **P1 B8. *(done — done 2026-11-16: full-month retrain, 30 traversal days (day 16 excluded by design, documented), 39.2M rows, 1.07M trips)* The edge model uses 18 of 31 days** (13 to 15 and 17 to 31 October, `docs/report.json`); the README says 19. Run the full month (`deployment/vm_bootstrap.sh`), then retest a weekday and weekend split, shrunk towards the pooled value, instead of seven separate days.
- [ ] **P2 B9. Leg lengths are straight lines between stops** (`api/app.py:134-140`, `workers/worker.js:426-427`), which is shorter than the road. Stage 03 already measures distance along the route shape for each traversal; export its median per edge.

## C. Planner results

- [x] **P1 C1. *(done — fixed 2026-11-16: best-per-route-sequence dedupe; verified live max=6 -> 6 distinct sequences)* Options repeat one itinerary.** Live: Kashmere Gate to Nehru Place with `max=6` returns 425UP three times and 0118EXT plus 425UP three times, differing only in the alighting or transfer stop. Keep the best option per route sequence (dedupe keys at `workers/worker.js:441` and `533`), then fill with different routes.
- [ ] **P1 C2. The search stops before it ranks.** The Worker keeps the first 30 direct options and the first 200 transfer candidates in nearest-stop order (`workers/worker.js:471-477`, `545`). The local API stops at 400 and lists at most 80 patterns per stop (`api/app.py:105`, `459`). A faster route found later is lost. Rank before cutting, or move to RAPTOR, which also allows two transfers.
- [ ] **P2 C3. There is no walk-only answer.** Live: a 250 m trip returns a 9.5-minute bus journey (about 4 minutes on foot at the planner's own walking speed), and a trip from a point to itself returns an 8.3-minute bus journey.
- [ ] **P2 C4. Walks of up to 2.5 km at each end are allowed** (`workers/worker.js:371`, `api/app.py:38`), and ranking is by total time alone. Cap walking near 1 km and add a penalty per transfer and per walking minute.
- [ ] **P2 C5. Worker transfers need the same stop.** The local API also allows a 350 m walk between stops (`api/app.py:517-524`). Port it.
- [ ] **P1 C6. `confidence` is a fixed string** even when the speed fell back to the route average or the 18 km/h default (`workers/worker.js:431-433`, `469`). Return the fallback level and sample count for each leg. The brief asks what the model does with little data; this makes the answer visible.
- [ ] **P2 C7. "Usual" time is the fastest of eight fixed hours** (`api/app.py:555-576`), including 22:00 and 23:00, when the route may not run. Define it as the off-peak median within service hours.

## D. API behaviour

Seen live unless marked.

- [x] **P1 D1. *(done — fixed 2026-11-16: space-as-+, signless offsets, bare local time as IST, garbage -> 400 with hint; verified live)* `depart` is fragile.**
  - `depart=2026-11-17T08:30:00+05:30` with the `+` not URL-encoded returns HTTP 500, "RangeError: Invalid time value". So does any value that cannot be parsed.
  - A value with no offset is read as UTC (08:30 comes back boarding at 08:36Z, which is 14:06 IST). The local API reads it as IST.
  - Fix at `workers/worker.js:366`: accept a space as `+`, default to IST, and return 400 with a message. The local API has the same uncaught parse (`api/app.py:425`, `434`; code).
- [x] **P1 D2. *(done — fixed 2026-11-16: p10/p90 include the final walk on the Worker)* p10 and p90 for direct journeys leave out the final walk** (`workers/worker.js:467-468`; `api/app.py:506-509` and `630-633`). Live: an option with a total of 9.5 min has a p90 of 7.5 min. The UI hides such bands; API users see them.
- [x] **P2 D3. *(done — fixed 2026-11-16: max clamped 1..6)* `max` is not validated.** `max=abc` and `max=0` return no options and no note; `max=-1` returns 59 options. Clamp it to 1 to 6 (`workers/worker.js:340`, `615`).
- [x] **P2 D4. *(done — partial 2026-11-16: out-of-region coordinates -> 400 (contract picked: unknown places stay a 200 + note))* Failures return HTTP 200.** An unknown place, an origin outside Delhi and `from=999,999` all return 200 with a note; the local API returns 404. Pick one contract and reject out-of-range coordinates with 400.
- [ ] **P3 D5. HTTP 500 responses carry the raw exception text** (`workers/worker.js:669`).
- [x] **P3 D6. *(done — fixed 2026-11-16: empty q -> [] (min length 2))* `/stops/search?q=` with an empty query returns 10 arbitrary stops** (`workers/worker.js:282-292`).
- [ ] **P3 D7. The Worker ignores `asOf` silently**, though the README lists it under the deployed API. (Code.)
- [ ] **P3 D8. There is no OpenAPI file for the deployed API.** FastAPI generates one locally; publish it with example responses.
- [ ] **P3 D9. `/journeys` sends no `cache-control`.** Identical queries in the same 15-minute bucket could be cached.

## E. Security and cost

- [x] **P1 E1. The Google key is sent to the browser.** (Code done: `/api/maps-config` now serves a separate `GOOGLE_MAPS_BROWSER_KEY`, same-origin only. Still to do in Google Cloud and Cloudflare: create the restricted browser key, set it as a secret, and rotate the old `GOOGLE_MAPS_KEY` because it was public.) Live: `/api/maps-config` returns a 39-character Google key to any origin (`workers/worker.js:144-148`). The README, the technical note (section 7.4) and the comment at `workers/worker.js:134` say it never leaves the server. The same secret pays for the server-side Places and Routes calls, protected only by a `Referer` header, which a caller can forge. Use two keys: a browser key limited to the Maps JavaScript API and your domains, and a server key limited to Places and Routes that is never served. The key's restrictions in Google Cloud were not visible to this review.
- [ ] **P1 E2. The billed Google proxies are open.** `/api/places/autocomplete`, `/api/places/resolve` and `/api/gmaps-drive` allow any origin and have no rate limit or cache (`workers/worker.js:27-31`, `156-249`). Add a Cloudflare rate limit, restrict CORS to your domains, cache drive times per origin-destination pair and 5-minute bucket, and send Places session tokens. (Code; the billed endpoints were not called.)
- [ ] **P3 E3. `vercel.json` sets only two security headers.** Add `Strict-Transport-Security`, `Permissions-Policy: geolocation=(self)` and a report-only CSP.
- [ ] **P3 E4. The local API puts `day` and `asOf` into file paths and an unbounded cache** (`api/app.py:246-286`, `703-705`). Accept only values listed by `/days`. (Code.)

## F. Validation

- [ ] **P1 F1. "Journey replay" scores single hops.** Each sample is one stop-to-next-stop traversal (`pipeline/03_project_trips.py:112-121`, sampled at `validation/holdout_journeys.py:49-52`). The 1.22 to 1.66 min MAE is per hop, and the mean hop time is not reported, so the figure has no scale. Build real journeys by chaining one trip's traversals between stops 5 to 40 apart, and score the ride time.
- [ ] **P1 F2. The journey test is in-sample.** Its test days (13, and 28 to 30 October) are in the training list in `docs/report.json`. The script's docstring names `--train-days` and `--test-days`; only `--days` exists. Train on 1 to 24 October and test on 25 to 31.
- [ ] **P2 F3. Scoring details in `validation/holdout_journeys.py`.** Every sample is queried as Tuesday 20 October (line 60). The route match is a substring test, so "12" matches "112" (lines 73 and 78). A fallback accepts the same route with different stops (lines 76 to 78).
- [ ] **P2 F4. "JNU" uses IIT Delhi's coordinates** in `validation/od_comparison.py:32` and `35` (both 28.5474, 77.1943).
- [ ] **P1 F5. Report what the brief judges.** For the 20 pairs, add an observed bus time (the median of archive trips serving both stops in that hour) beside your estimate and Google's. Report error by hour band and trip length, band coverage, and a timetable baseline.
- [ ] **P3 F6. `validation/merge_gmaps.py` holds hard-coded round-number Google times.** Whether anything still uses it was not checked; remove it if not.

## G. Repository and reproducibility

- [ ] **P2 G1. No tests and no CI.** The Python and JavaScript planners have drifted apart. Add a small set of fixed queries run against both, and a unit test for the hour conversion, which would have caught A1.
- [ ] **P2 G2. Dependencies are unpinned** (`requirements.txt` uses `>=`). `lightgbm` is never imported. `python-dotenv` is imported (`api/app.py:21`) but only arrives through `uvicorn[standard]`. Pin versions and add a lock file.
- [ ] **P3 G3. `.env.example` is out of date.** It lacks `GTFS_RT_URL` (`api/app.py:46`) and `GOOGLE_MAPS_MAP_ID` (`workers/worker.js:147`), and lists `WEATHER_API_KEY` and `COREWEAVE_LLM_*`, which no code reads.
- [ ] **P2 G4. Setup assumes one Windows machine.** The default archive path is `C:/Users/praya/Downloads/...` (`pipeline/common.py:16`), the quick start is PowerShell only, and no script fetches the static GTFS. Add a cross-platform runner and a GTFS download step.
- [ ] **P3 G5. There is no LICENSE file**, though the README calls the project open source.
- [ ] **P3 G6. D1 is bound and loaded but never queried.** `workers/wrangler.toml` binds it and the README deploy step fills it; `workers/worker.js` reads everything from `model.json`. Use it for the edge cells (A2) or remove it.
- [ ] **P3 G7. There are two UIs.** `web/` is an older one still served by the local API at `/web`; `workers/public` is the current one. Serve one.
- [ ] **P3 G8. Personal paths and identity are in the repo** (`docs/vm-instructions.md`, `deployment/finalize_gcp.ps1`), along with six QA screenshots in `docs/`.

## H. Ideas, not defects

- Poll GTFS-RT from the Worker on a Cron Trigger and store recent positions, so the deployed planner has live speeds (A6).
- Show the next real bus at the boarding stop from live positions, in place of the average wait.
- Put the search in the page URL so a result can be shared; the planner keeps no state in the URL today.
- Add Metro legs; the brief welcomes multimodal journeys.
- Offer "arrive by" as well as "leave at".
- Add Hindi stop names and labels.
