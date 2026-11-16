#!/usr/bin/env bash
# Delhi Pulse — VM bootstrap: full-month (Oct 1-31) model run.
#
# Expects (placed by the operator before this script runs):
#   /opt/delhi-pulse/          code payload untarred (pipeline/ api/ validation/
#                              deployment/ data/gtfs/)
#   /opt/delhi-pulse/data/clean/traversals_*.parquet   already-projected days
#
# Produces:
#   /opt/delhi-pulse/model/                      retrained model tables
#   /opt/delhi-pulse/workers/public/model.json(.gz)  edge artifact
#   /opt/delhi-pulse/docs/   report.json, d1_part*.sql, overnight.log
#   /opt/artifacts.tar.gz                        everything, ready to pull back
set -euo pipefail

cd /opt/delhi-pulse
export DP_ARCHIVE=/opt/archive/delhi-dtc-bus-gtfs-rt-2026-10.7z
export DEBIAN_FRONTEND=noninteractive
LOG() { echo "[$(date -u '+%H:%M:%S')] $*"; }
echo $$ > /opt/bootstrap.pid

mkdir -p data/raw data/clean model workers/public docs

# 1) system deps ---------------------------------------------------------------
apt-get update -qq >/dev/null
apt-get install -y -qq python3-venv python3-pip p7zip-full curl >/dev/null
LOG "apt deps ok"

# 2) python env ----------------------------------------------------------------
if [ ! -d /opt/venv ]; then
  python3 -m venv /opt/venv
  /opt/venv/bin/pip install -q --upgrade pip
  /opt/venv/bin/pip install -q duckdb pandas numpy pyarrow py7zr \
    requests httpx python-dotenv fastapi
fi
LOG "python env ok"

# 3) archive --------------------------------------------------------------------
mkdir -p /opt/archive
URL=https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z
# resume-proof loop: curl error 18 (transfer closed) is NOT auto-retried by
# --retry, so we loop ourselves and -C - resumes byte-exact every time.
ok=""
for i in $(seq 1 40); do
  if curl -sS --retry-all-errors --retry 5 --retry-delay 2 -C - \
       -o "$DP_ARCHIVE" "$URL"; then ok=1; break; fi
  LOG "download interrupted (attempt $i) — resuming from $(stat -c%s "$DP_ARCHIVE" 2>/dev/null || echo 0) bytes"
  sleep 2
done
if [ -z "$ok" ] || [ "$(stat -c%s "$DP_ARCHIVE" 2>/dev/null || echo 0)" -lt 15000000000 ]; then
  LOG "archive FAILED after retries: $(stat -c%s "$DP_ARCHIVE" 2>/dev/null || echo 0) bytes"
  exit 1
fi
LOG "archive ready: $(du -h "$DP_ARCHIVE" | cut -f1)"
# spot integrity test (single known-good member; the global SHA is absent)
7z t "$DP_ARCHIVE" bus_movements_2026_10_01.db >/dev/null 2>&1 \
  || { LOG "7z integrity test FAILED"; exit 1; }
LOG "7z integrity ok"

# 4) extract + clean every missing day (8-way parallel, raw DBs deleted) --------
cat > /opt/extract_one.sh <<'EOF'
#!/usr/bin/env bash
set -e
d=$1
cd /opt/delhi-pulse
[ -s "data/clean/pings_$d.parquet" ] && exit 0
/opt/venv/bin/python pipeline/01_extract_day.py --day "$d"
/opt/venv/bin/python pipeline/02_clean_pings.py --day "$d"
rm -f "data/raw/bus_movements_$d.db" "data/raw/bus_movements_$d.db-shm" \
      "data/raw/bus_movements_$d.db-wal"
echo "cleaned: $d"
EOF
chmod +x /opt/extract_one.sh
# garbage-collect raw DBs of days already cleaned during interrupted runs
for f in data/raw/bus_movements_*.db; do
  d=$(basename "$f" .db | sed 's/bus_movements_//')
  if [ -s "data/clean/pings_$d.parquet" ]; then
    rm -f "$f" "$f-shm" "$f-wal" && LOG "gc: raw $d (pings already clean)"
  fi
done
XARGS_KO=0
for i in $(seq -w 1 31); do echo "2026_10_$i"; done \
  | xargs -P 8 -I{} /opt/extract_one.sh {} \
  || { XARGS_KO=1; LOG "pass-1 workers failed; retrying missing serially"; }
# retry pass, serial and gentle
for i in $(seq -w 1 31); do
  d="2026_10_$i"
  [ -s "data/clean/pings_$d.parquet" ] || /opt/extract_one.sh "$d" \
      || LOG "STILL FAILED after retry: $d"
done
N=$(ls data/clean/pings_2026_10_*.parquet 2>/dev/null | wc -l)
LOG "clean pings ready: $N/31"
[ "$N" -ge 31 ] || { LOG "EXTRACT INCOMPLETE ($N/31) — aborting refresh"; exit 1; }

# 5) full-month run: stage-03 for missing days + entire refresh chain -----------
LOG "starting overnight.py (projection for missing days, then retrain) ..."
/opt/venv/bin/python pipeline/overnight.py
LOG "overnight.py finished"

# 6) edge artifact --------------------------------------------------------------
/opt/venv/bin/python deployment/export_model_json.py
LOG "edge model built"

# 7) pack artifacts --------------------------------------------------------------
tar czf /opt/artifacts.tar.gz \
  model \
  workers/public/model.json workers/public/model.json.gz \
  docs/overnight.log docs/report.json docs/d1_part*.sql \
  data/clean/traversals_*.parquet \
  data/clean/passages_*.parquet data/clean/headway*.parquet \
  data/clean/speeds_*.parquet data/clean/speedsagg_*.parquet
chmod 644 /opt/artifacts.tar.gz
LOG "ALL DONE — artifacts: $(du -h /opt/artifacts.tar.gz | cut -f1)"
