#!/usr/bin/env bash
# Sync model + small-data artifacts to Cloudflare R2 (bucket: delhi-pulse).
# Requires: npx wrangler login  (once)
# Usage:    bash deployment/r2_sync.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BUCKET=delhi-pulse
R2() { npx --yes wrangler@latest r2 object put "$BUCKET/$1" --file "$2" --remote; }

for f in model/*.parquet model/*.json; do
  [ -f "$f" ] || continue
  echo "-> $f"
  R2 "$f" "$f"
done

for f in data/clean/speeds_*.parquet data/clean/stats_*.json; do
  [ -f "$f" ] || continue
  echo "-> $f"
  R2 "$f" "$f"
done

for f in data/gtfs/*.txt; do
  [ -f "$f" ] || continue
  echo "-> $f"
  R2 "gtfs/$(basename "$f")" "$f"
done

echo "R2 sync complete."
