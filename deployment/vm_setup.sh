#!/usr/bin/env bash
# Delhi Pulse — one-shot CoreWeave VM bootstrap
# Usage:  bash deployment/vm_setup.sh          (processes $DAYS or a starter week)
#         DAYS="2026_10_13 2026_10_14 ..." bash deployment/vm_setup.sh
#         KEEP_RAW=1 bash deployment/vm_setup.sh # don't delete raw day DBs
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
URL="https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z"
SUMS="https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS"

echo "== [vm] system packages =="
if ! command -v python3 >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip sqlite3 git curl p7zip-full
fi

echo "== [vm] python env =="
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== [vm] data download =="
mkdir -p data
if [ ! -f data/delhi-dtc-bus-gtfs-rt-2026-10.7z ]; then
  curl -L -C - --retry 30 --retry-delay 5 --retry-all-errors \
       -o data/delhi-dtc-bus-gtfs-rt-2026-10.7z "$URL"
fi
if [ ! -f data/SHA256SUMS ]; then curl -sS -o data/SHA256SUMS "$SUMS"; fi
grep delhi-dtc-bus-gtfs-rt data/SHA256SUMS | ( cd data && sha256sum -c - ) || \
  echo "WARNING: checksum mismatch — re-download advised"

echo "== [vm] GTFS check =="
if [ ! -f data/gtfs/stops.txt ]; then
  echo "!! data/gtfs/stops.txt missing."
  echo "!! Copy the Transport Stack static GTFS .txt files into data/gtfs/ and re-run."
  exit 3
fi

DAYS=${DAYS:-"2026_10_13 2026_10_14 2026_10_15 2026_10_16 2026_10_17"}
for DAY in $DAYS; do
  echo "== [vm] pipeline day $DAY =="
  .venv/bin/python pipeline/01_extract_day.py --day "$DAY"
  .venv/bin/python pipeline/02_clean_pings.py --day "$DAY"
  [ -z "${KEEP_RAW:-}" ] && rm -f "data/raw/bus_movements_${DAY}.db"
  .venv/bin/python pipeline/03_project_trips.py --day "$DAY"
done

echo "== [vm] train =="
.venv/bin/python pipeline/04_train_model.py

echo "== [vm] serve =="
echo "API -> http://<vm-ip>:8000   UI -> http://<vm-ip>:8000/web"
exec .venv/bin/python -m uvicorn api.app:app --host 0.0.0.0 --port 8000
