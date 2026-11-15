"""Validate the real static GTFS + critical join checks against ping data."""
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GTFS, CLEAN

print("== counts ==")
for f in ("stops", "routes", "trips", "stop_times", "shapes"):
    df = pd.read_csv(GTFS / f"{f}.txt", low_memory=False)
    print(f"  {f}: {len(df):,} rows; cols: {list(df.columns)[:8]}")

routes = pd.read_csv(GTFS / "routes.txt", low_memory=False)
trips = pd.read_csv(GTFS / "trips.txt", dtype=str, low_memory=False)
stops = pd.read_csv(GTFS / "stops.txt", low_memory=False)

print("\n== route_id dtype/format ==")
print("  routes head:", routes.route_id.head(4).tolist())
print("  trips route_id dtype sample:", trips.route_id.head(4).tolist())
print("  trips shape_id sample:", trips.shape_id.head(3).tolist())
print("  trip_id sample:", trips.trip_id.head(3).tolist())
print("  trips with shape_id:", trips.shape_id.notna().sum(), "/", len(trips))

print("\n== critical join: ping route_ids vs GTFS routes ==")
ping_routes = duckdb.connect().execute(f"""
    SELECT DISTINCT route_id FROM read_parquet(
        '{(CLEAN / 'pings_2026_10_13.parquet').as_posix()}')
""").df().route_id.dropna().astype(int)
gtfs_routes = set(pd.to_numeric(routes.route_id, errors="coerce").dropna().astype(int))
hit = ping_routes.isin(gtfs_routes).sum()
print(f"  ping routes: {len(ping_routes):,}  matched to GTFS routes: {hit:,} "
      f"({100*hit/len(ping_routes):.1f}%)")

print("\n== stop sanity (lat/lng ranges) ==")
print("  lat", stops.stop_lat.min(), "..", stops.stop_lat.max(),
      " lng", stops.stop_lon.min(), "..", stops.stop_lon.max())

print("\n== service-jaunt: patterns ==")
st = pd.read_csv(GTFS / "stop_times.txt", dtype=str, low_memory=False)
trippat = st.groupby("trip_id").stop_id.apply(lambda s: hash(tuple(s)))
print("  stop_times rows:", f"{len(st):,}", " distinct trip patterns:", f"{trippat.nunique():,}")

# speed: do bus trip labels' route prefix match trips' own route_id space?
print("\n== shape coverage for ping-matched routes ==")
tr = trips.copy()
tr["route_int"] = pd.to_numeric(tr.route_id, errors="coerce")
matched = tr[tr.route_int.isin(set(ping_routes))]
w_shape = matched.shape_id.notna().sum()
print(f"  matched trips: {len(matched):,}; with shape_id: {w_shape:,} "
      f"({100*w_shape/max(1,len(matched)):.0f}%)")
