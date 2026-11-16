# Delhi Pulse — fix plan (fleet 2 working from this doc)

Branch `retrain/full-month` @ `b90544f`. Thread state: holdout rerun in
background, audited fleet 1 reports. Goal: ship-row the remaining backlog.

Hard rules for every agent:
1. Own your listed files EXCLUSIVELY — do not edit outside them. Report diffs; I merge, deploy, commit.
2. Read `docs/backlog.md` + your section below first.
3. Syntax-check every touched file (`node --check` for JS, `.venv\Scripts\python.exe -c "import ast ..."` for PY) before reporting.
4. Frozen API contract (`docs/v0-brief.md`): fields may be ADDED, never renamed/removed.
5. On the live Worker: read-only probes only, zero abuse volume; NEVER hammer billed proxies.

## A — owns `workers/worker.js`
* A7 fuzzy stop match in `resolve()` + `/stops/search` scoring (3x substring + 2x all-token-prefix + 1x token-overlap; exact match short-circuit; top 10).
* B7-band consume `M.band_cal` if present (from export agent) in `bandOf()`.
* C5 walk transfers ≤350 m (ported from `api/app.py` xfer_neighbors path), memo 15 unique transfer stops/request.
* C6 per-leg `source` ("route-hour"|"route-avg"|"city") + `n` (from M.speeds_n) — additive.
* D9 `cache-control: public, max-age=300` on /journeys with a depart in the future; `no-store` if default now.
* C2-lite: cheap-cost pre-scored candidate estimate phase before the full computation (heap top-N) if permitted by CPU budget; cap exploration to same-cost envelope: treat as optional/stetch if time.
* E2-final: session tokens for Places (UI sends `X-Session-Token` from crypto.randomUUID — see UI hint below, and keep your change backward-compatible); autocomplete input min ≥3 chars (Done?) re-verify.
* Get `origins` list constrained to Array.includes([worker host, vercel host, localhost/127]) with null-origin banned for billed only, per report from security agent 6.

## B — owns `deployment/export_model_json.py` + `pipeline/04g_headways.py`
* B5 exporter: 04g emit per-(route,hour) observed gaps `gap_mean_s`,`gap_cv`; export writes `M.wait_gap` `{route:{hour:[wait_mean,cv]}}` plus existing headway.
* C6 exporter: write `M.speeds_n` (route→hour→n) and `route_avg_n` (route→n). n values should be integer-truncated (bytes).
* C7 exporter: `usual_hour` = median of the off-peak candidate speeds (11-17 ∩ speed ∩ headway-backed) — median, not fastest; NO widening outside window.
* B7 exporter: compute band calibration factors from `model/holdout_journeys.json` (if `band_coverage` present) or fixed 1.0 default; export `M.band_cal = [lo_factor, hi_factor]`.
* Missing-file guards: if `route_hour.parquet` absent, do NOT silently fall back to `_real` — print a warning and proceed with `source` tag noted in model.json meta fields (`{"model_source": "traversal"|"ping-pair"}`) so downstream tests can assert.
* Top: gate `detour` export with ≥2 effective edges per route (chord_m>0) else omit key.

## D — owns `validation/holdout_journeys.py`
* Add per-leg p10/p90 passthrough (from t_edge q10/q90 columns, same shrink formula), compute `band_coverage` = share of journeys with `pred_p10 ≤ ride_s ≤ pred_p90` reported in the JSON output. Reuse predicted-side aggregation as `additive`.
* Export as `model/holdout_journeys.json` key `band_coverage` (float, e.g. 0.87 or 0.83).
* Keep the segmentation fix already applied (day + >600 s gap) and `band()` label cleanups.
* Match the model knobs precisely: import `MIN_CELL_N, SHRINK_K, DENSE_N` from `pipeline/common.py` and reference them in the SQL/template.

## E — owns `validation/hold_pings.py` + `validation/build_report.py`
* hold_pings: compute dow arm on the INTERSECTION subset (rows where both arms have predictions), report `lift_dow_vs_pooled` on that subset + also `lift_dow_vs_pooled_allrows`.
* hold_pings: add pair-count-weighted MAE alongside unweighted.
* build_report: fix schema drift explicitly (`hold_pings.mae_model_kmh`, `holdout_pings.json` keys, `holdout_journeys.json` keys), hard-fail on missing key errors instead of blanket excepts.
* build_report: if holdout_journeys.json missing or stale → insert `"journeys": null` with explicit `"error": "holdout_journeys missing or stale"` in report.

## F — owns `deployment/export_d1.py` + `deployment/split_sql.py`
* Prefer traversal tables over *_real for D1 speed tables; stamp `source=` in `D1 rt` fields; `meta` row for `family`.
* NaN guards in esc() (None|NaN|Inf → NULL), missing-file existence checks, atomic timestamp assert (export+split in same main() invocation).
* split_sql: cut ONLY at table boundaries (start new part at DROP TABLE), each part must end cleanly; parts ≤ 4 MB and independently importable.

## G — owns NEW `validation/pair_baselines.py`
* Implement the design from fix-plan §F5 (stop mapping, observed bus trip medians, GTFS timetable minimum), output `docs/pair_baselines.json` keyed by `(origin, destination)` with all `dep_hour` resolutions, `p50_s`, `n`, plus scheduled times.
* Run it (background DuckDB, low memory) but PRINT only summaries without dumping huge data on the wire.
* Do not edit docs/technical-note.md — report the numbers so I can merge them in.

## H — owns `tests/` + NEW `workers/smoke.mjs` + `.github/workflows/ci.yml`
* tests/test_api_invariants.py — pure-python unit tests of `pipeline/common.py` helpers (hour/dow, MIN_CELL_N etc.) + holdout tools; run with `pytest tests/ -q` and confirm green.
* workers/smoke.mjs — Node script hitting the deployed edge endpoints and asserting invariants (options monotonic, eta_p10≤p50≤p90, max clamp, note prepend), writing `tests/golden/edge.json`. No auth, no stress, read-only probes at ~10 requests total; endpoints under threshold: journeys, status, health, pulse, stops/*; DO NOT touch places/gmaps-drive.
* ci.yml — single job, pytest + compileall + node smoke (use a repository secret `EDGE_URL`, keep `continue-on-error: true` for smoke since CI has no secrets by default).

## I — owns `README.md` + `LICENSE` + `requirements.txt` + `Makefile` + `.gitignore`
* LICENSE (MIT, "Delhi Pulse contributors").
* requirements.txt: pin from working venv versions (I confirmed: duckdb 2.0.0 pandas 3.1.1 numpy 2.4.6 pyarrow 26.0.1 fastapi 0.163.1 uvicorn 0.54.0 httpx 0.28.1 requests 2.34.2 py7zr 1.1.4 python-dotenv 1.2.4 huggingface-hub 0.26.5 rapidfuzz 3.14.6 gtfs-realtime-bindings 2.0.0); remove lightgbm/pytz/shapely; add requests/huggingface_hub/python-dotenv pins; mark cwsandbox as `# optional: CoreWeave runs`.
* Makefile — `make fetch-archive / model / serve / deploy` on Windows-compatible commands (python -m etc.).
* README quickstart: cross-platform (venv + pip, no `C:/Users` paths), add HF-model shortcut note ("model/*.parquet from the HF dataset skips the 15 GB pipeline"), correct the D1 import step to all three parts.
* .gitignore: verify `.tmp/`, `.wrangler/`, `dev/`, `.env*`, `data/`, `model/` coverage; add QA png removal note (moving PNGs out of tracked docs belongs to agent N — skip).
* README badges: MIT badge + edge URL only after LICENSE+ Makefile add.

## K — owns `docs/v0-brief.md`
* §3.1: add `route_code` (new field), `confidence` per-type parity (local API differs — say so), add `usual_*` and `route_code` to the example block; correct eta band example numbers to current deployed values (71.2, 54.2, 90.2) if any; add the zero-option example `{options: [], note: "no route found within 1 transfer"}`.
* §5.1: `/status` routes 2245, edges 853480.
* §3.4: add `stride` parameter, `max_age` cache, docs `asOf` → note as local-only.
* §2.2: swap/UV constraint: document that the deployed UI *replaces* MapLibre with Google Maps JS + browser key via `/api/maps-config` (the restriction was the pre-redesign assumption; keep as an editorial note so the doc is not self-contradictory).
* §7: API-base rules for external hosts (billed proxies origin-gated; read API still CORS *).

## L — owns `docs/technical-note.md`
Apply the honesty audit items P1-1..P1-7 and P2s EXCEPT §7.3 numbers (agent G provides them):
* §7.1 label: "31 leave-one-day-out folds (ping-pair layer — NOT the served edge model). Add note on arm coverage: "served traversal arm is not yet LODO-scored".
* §7.2: replace with the journey-level holdout results quote-marking "pending", then the new small-coverage honest wording (72/36 samples → "n=36 scored" caveat).
* §7.3: replace "rush/quiet" → "09:00 comparison slot"; max ratio 2.25 with JNU footnote, 10-of-20 transfers number.
* §1/§2/§5: route-count split (2,245 served / 2,303 speed-layer), pct_kept range 56.4-66.3%.
* §8: fix error-of-reason wording on DOW ("31-day retest, dow +9.8%").

## M — owns `docs/backlog.md`
* Refresh header: "reviewed 16 Nov 2026 at 96127f4 — main items fixed same day on retrain/full-month".
* Tick all items covered by today's commits/deploys: A1 A2 A3 A4 A5 A9 B1 B2 B4 B6 B8 B9 C1 C3 D1 D2 D3 D4 D5 D6 D9(partial headers — keep partial as documented) E2(partial) F1 F2 F6 G3 with the same short "done —" notes.
* Mark E1 as partially delivered: code-side split + docs updated, rotation of the old key is an owner action in the Google console — do NOT tick as fully done.
* leave C2, C5-C7, B3, B5, B7 pending (fixers in flight D-G).
* Fix: "Nothing on this list has been fixed yet" header.

## N — owns `deployment/server.yaml`, `deployment/finalize_gcp.ps1`, `docs/vm-instructions.md`, `pipeline/qa_*.mjs`, `pipeline/common.py` default path
* Delete the rev-exported public SSH key + personal paths from tracked files (vm-instructions + server.yaml)-decide best: git rm + PR note or redact.
* finalize_gcp.ps1: parameterize GCP project/zone via env (`$env:GCP_PROJECT`) with defaults removed; replace any personal paths.
* qa_*.mjs: use relative `docs/qa_*.png` paths.
* pipeline/common.py DP_ARCHIVE default → `ROOT/data/delhi-... .7z` (uses Path.home() only as comment), plus exporter/tmp dir mention.

## Hunters (explore only, no edits)
H1) live red-team full flow (~12 read-probes, deduping similar pairs, assert no heavy actions on billed), looking for anomalies beyond earlier reports (input parsing edge cases, model key collisions, geo straddling).
H2) git history forensics: dangling blobs with secrets, lfs pointer issues, huge blobs beyond D1-sql already known, branch structure sanity, orphaned refs. Report only.
H3) cost forensics: estimate current burn per 100 demo journeys across journeys+drive+places; recommend hard code limits (400/day tally in KV?) and quota provisioning steps in the Google console. 
H4) exploit-hunter on the UI JS surface for surface XSS via place names (any innerHTML sinks? text seems app-generated; verify), URL injection into map URLs, i18n paths.
H5) data quality: passages day-16 exclusion footprint + top-5 stop id collisions and route label misparse classes; report.
H6) evidence gaps: spot-check our technical-note claims against a read-only pass in the corpus (report claims that lack artifacts on disk).
H7) contract2: grep the v0-brief and README for anything not actually in the repo (e.g., dataset links, model card, LFS pointers).
H8) "demo-finder": propose 12 demo-route queries with expected outputs (the most impressive planner queries to show; quantify key values, e.g., the 0.95× beat-car pair and dead-hour honesty), to be used for the 4-min video script.

Sequence after agent return values: worker team agent's changes → I remerge + deploy → live re-run acceptance → docs+backlog PR submission.
