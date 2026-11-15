"""
Stage 4: train the v0 travel-time model from edge traversals.

Hierarchy (fallback chain for the planner):
  1. (route_id, from_stop, to_stop, hour)  -> median traversal seconds  [edge_hour]
  2. (route_id, hour)                      -> observed speed km/h        [route_hour]
  3. (route_id)                            -> observed speed km/h        [route_speed]
  4. (hour)                                -> city-wide speed km/h       [city_hour]

Outputs:
  model/edge_hour.parquet   route_id, from_stop, to_stop, hour_ist, p50_s, n
  model/route_hour.parquet  route_id, hour_ist, speed_kmh, n
  model/route_speed.parquet route_id, speed_kmh, n
  model/city_hour.parquet   hour_ist, speed_kmh, n
  model/meta.json
Usage:
  python pipeline/04_train_model.py
"""
import json
import datetime

import duckdb
import pandas as pd

from common import ensure_dirs, CLEAN, MODEL


def main():
    ensure_dirs()
    srcs = sorted(CLEAN.glob("traversals_*.parquet"))
    if not srcs:
        raise SystemExit("[04] no traversals_*.parquet found — run 03 first")
    files = ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in srcs)
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")

    edge_hour = con.execute(f"""
        SELECT route_id, from_stop, to_stop, hour_ist,
               quantile_cont(dur_s, 0.5) AS p50_s,
               COUNT(*) AS n
        FROM read_parquet([{files}])
        GROUP BY 1,2,3,4 HAVING COUNT(*) >= 3
    """).df()

    route_hour = con.execute(f"""
        SELECT route_id, hour_ist,
               SUM(dist_m) / NULLIF(SUM(dur_s),0) * 3.6 AS speed_kmh,
               COUNT(*) AS n
        FROM read_parquet([{files}])
        GROUP BY 1,2
    """).df()

    route_speed = con.execute(f"""
        SELECT route_id,
               SUM(dist_m) / NULLIF(SUM(dur_s),0) * 3.6 AS speed_kmh,
               COUNT(*) AS n
        FROM read_parquet([{files}])
        GROUP BY 1
    """).df()

    city_hour = con.execute(f"""
        SELECT hour_ist,
               SUM(dist_m) / NULLIF(SUM(dur_s),0) * 3.6 AS speed_kmh,
               COUNT(*) AS n
        FROM read_parquet([{files}])
        GROUP BY 1 ORDER BY 1
    """).df()

    trips = con.execute(f"SELECT COUNT(DISTINCT trip_id) n FROM read_parquet([{files}])").fetchone()[0]
    rows = con.execute(f"SELECT COUNT(*) n FROM read_parquet([{files}])").fetchone()[0]

    edge_hour.to_parquet(MODEL / "edge_hour.parquet", index=False)
    route_hour.to_parquet(MODEL / "route_hour.parquet", index=False)
    route_speed.to_parquet(MODEL / "route_speed.parquet", index=False)
    city_hour.to_parquet(MODEL / "city_hour.parquet", index=False)

    meta = {
        "version": "v0-medians",
        "generated_utc": datetime.datetime.utcnow().isoformat(),
        "days": [p.stem.replace("traversals_", "") for p in srcs],
        "n_traversal_rows": int(rows),
        "n_trips_observed": int(trips),
        "n_edge_hour_cells": int(len(edge_hour)),
        "n_routes": int(route_speed.route_id.nunique()),
        "quantiles": "p50 only (v0); p10/p90 arrive with the LightGBM pass",
    }
    (MODEL / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[04] model v0 trained: {len(edge_hour):,} edge×hour cells, "
          f"{route_speed.route_id.nunique()} routes, {trips:,} observed trips")
    if len(city_hour):
        am = city_hour[city_hour.hour_ist == 9].speed_kmh
        pm = city_hour[city_hour.hour_ist == 21].speed_kmh
        if len(am) and len(pm):
            print(f"[04] sanity: 09:00 speed {am.iloc[0]:.1f} km/h vs "
                  f"21:00 {pm.iloc[0]:.1f} km/h (peak should be slower)")


if __name__ == "__main__":
    main()
