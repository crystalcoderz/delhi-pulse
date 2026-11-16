# Delhi Pulse — cross-platform Makefile (GNU make; cmd, PowerShell, bash all OK).
#
# Every target runs `python -m ...` / `python <script>` with the interpreter in
# $(PY), so nothing depends on shell-specific syntax or on __pyshim paths.
#
# First run:
#   make setup
#   .venv\Scripts\activate          (Windows cmd/PowerShell)
#   source .venv/bin/activate       (macOS / Linux / git-bash)
# then either path:
#   make model && make serve        # trained tables from HF, skip the 15 GB run
#   make fetch-archive && make train-month
# Edge deploy:
#   make deploy
#
# Point at an interpreter explicitly if you skipped activation, e.g.
#   make serve PY=.venv/Scripts/python.exe

PY      ?= python
NPX     ?= npx
DAY     ?= 2026_10_13
PORT    ?= 8010
HF_REPO ?= PRAYAASOG/delhi-pulse-model

.PHONY: setup fetch-archive model model-train train train-month serve deploy check

setup:
	$(PY) -m venv .venv
	$(PY) -m pip --python .venv install --upgrade pip
	$(PY) -m pip --python .venv install -r requirements.txt

# --- data ------------------------------------------------------------------
# 15 GB October archive from R2, SHA256-verified, into data/ (gitignored).
fetch-archive:
	$(PY) -c "import sys, pathlib, hashlib, urllib.request as u; sys.path.insert(0, 'pipeline'); from common import DATA_URL, SUMS_URL; UA = {'User-Agent': 'delhi-pulse/1.0'}; d = pathlib.Path('data'); d.mkdir(exist_ok=True); name = DATA_URL.rsplit('/', 1)[-1]; p = d / name; print('downloading', DATA_URL, flush=True); r = u.urlopen(u.Request(DATA_URL, headers=UA)); f = p.open('wb'); [f.write(chunk) for chunk in iter(lambda: r.read(1 << 20), b'')]; f.close(); sums = u.urlopen(u.Request(SUMS_URL, headers=UA)).read().decode(); want = [l.split()[0] for l in sums.splitlines() if name in l]; got = hashlib.file_digest(p.open('rb'), 'sha256').hexdigest(); ok = (not want) or got == want[0]; print('sha256 ' + got + (' OK' if ok else ' MISMATCH expected ' + want[0])); raise SystemExit(0 if ok else 1)"

# --- trained tables --------------------------------------------------------
# Shortcut: pull model/*.parquet + model/*.json from the HF dataset (no pipeline run).
model:
	$(PY) -m huggingface_hub.commands.huggingface_cli download $(HF_REPO) --repo-type dataset --include "model/*.parquet" "model/*.json" --local-dir .

# Full rebuild over every processed day (stages 04f -> 05 -> 04 -> 04g + holdouts).
model-train:
	$(PY) pipeline/refresh_model.py

# --- one day through the pipeline -----------------------------------------
train:
	$(PY) pipeline/01_extract_day.py --day $(DAY)
	$(PY) pipeline/02_clean_pings.py --day $(DAY)
	$(PY) pipeline/03_project_trips.py --day $(DAY)
	$(PY) pipeline/04_train_model.py

# Whole month: project every extracted day, then the refresh chain. Long.
train-month:
	$(PY) pipeline/overnight.py
	$(PY) pipeline/refresh_model.py

# --- serve ----------------------------------------------------------------
serve:
	$(PY) -m uvicorn api.app:app --port $(PORT)

# --- edge (Cloudflare Workers + D1) --------------------------------------
# export_d1.py writes docs/d1_export.sql and splits it into docs/d1_partN.sql
# in the same process (stamp-asserted, so a stale export cannot be sharded).
# Then import ALL parts in order -- importing only part1 leaves the edge model
# incomplete.
deploy:
	$(PY) deployment/export_d1.py
	cd workers && $(NPX) --yes wrangler@latest d1 execute delhi-pulse --remote --file ../docs/d1_part1.sql
	cd workers && $(NPX) --yes wrangler@latest d1 execute delhi-pulse --remote --file ../docs/d1_part2.sql
	cd workers && $(NPX) --yes wrangler@latest d1 execute delhi-pulse --remote --file ../docs/d1_part3.sql
	cd workers && $(NPX) --yes wrangler@latest deploy

# --- sanity ---------------------------------------------------------------
check:
	$(PY) -m compileall -q pipeline api validation deployment
	$(PY) -m pytest tests -q
