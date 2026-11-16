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
  DP_DUCK_MEM=36GB DP_DUCK_THREADS=8  (host-specific; defaults fit a laptop)
"""
import datetime
import json
import os

import duckdb
import pandas as pd

from common import ensure_dirs, CLEAN, MODEL, DENSE_N


def main():
    ensure_dirs()
    srcs = sorted(CLEAN.glob("traversals_*.parquet"))
    if not srcs:
        raise SystemExit("[04] no traversals_*.parquet found — run 03 first")
    files = ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in srcs)
    con = duckdb.connect()
    # tuned for the training host via env; defaults keep a laptop alive.
    # The 6GB default on the 48GB VM forced a >60GiB disk spill and OOMed
    # the first full-month run (see overnight.log 2026-11-16 16:15).
    con.execute("SET memory_limit='{}'".format(
        os.environ.get("DP_DUCK_MEM", "6GB")))
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET threads={}".format(
        int(os.environ.get("DP_DUCK_THREADS", max(1, (os.cpu_count() or 4) // 2)))))

    # B6: quantile floor + shrinkage. Cells with n < 5 are dropped; sparse
    # cells (n 5..24) are shrunk toward the time implied by the route-hour
    # speed with a pseudo-count of 10, and their bands shrink by n/(n+10),
    # so a 3-sample cell can no longer dictate an extreme median.
    # Memory fix: the three shrink CTEs each read the 40M-row corpus
    # stream; on a 16GB laptop all three together OOM even at 13GB.
    # Materialize each aggregate into a temp table SEQUENTIALLY — one
    # pass at a time — so peak memory is one phase's output, not three.
    MIN_CELL_N = int(os.environ.get("DP_EDGE_MIN_N", "5"))
    SHRINK_K = 10

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t_raw AS
        SELECT route_id, from_stop, to_stop, hour_ist,
               quantile_cont(dur_s, 0.10) AS q10_raw,
               quantile_cont(dur_s, 0.50) AS q50_raw,
               quantile_cont(dur_s, 0.90) AS q90_raw,
               COUNT(*) AS n
        FROM read_parquet([{files}])
        GROUP BY 1,2,3,4 HAVING COUNT(*) >= {MIN_CELL_N}
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t_rh AS
        SELECT route_id, hour_ist,
               SUM(dur_s) AS rdur, SUM(dist_m) AS rdist
        FROM read_parquet([{files}])
        GROUP BY 1,2
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t_ed AS
        SELECT route_id, from_stop, to_stop, median(dist_m) AS ede
        FROM read_parquet([{files}])
        GROUP BY 1,2,3
    """)

    edge_hour = con.execute(f"""
        WITH imp AS (
            SELECT c.*, e.ede * r.rdur / NULLIF(r.rdist, 0) AS imp_s
            FROM t_raw c
            JOIN t_rh r USING (route_id, hour_ist)
            JOIN t_ed e USING (route_id, from_stop, to_stop)
        )
        SELECT route_id, from_stop, to_stop, hour_ist,
               LEAST(p10s, p50s) AS p10_s,
               p50s AS p50_s,
               GREATEST(p90s, p50s, LEAST(p10s, p50s) * 1.05) AS p90_s,
               n
        FROM (
            SELECT route_id, from_stop, to_stop, hour_ist, n,
                   CASE WHEN n >= {DENSE_N} OR imp_s IS NULL THEN q50_raw
                        ELSE (n * q50_raw + {SHRINK_K} * imp_s)
                             / (n + {SHRINK_K}) END AS p50s,
                   CASE WHEN n >= {DENSE_N} OR imp_s IS NULL
                        THEN GREATEST(q10_raw, 0.4 * q50_raw)
                        ELSE GREATEST(0.4 * q50_raw,
                             p50s_raw - (q50_raw - q10_raw) * n / (n + {SHRINK_K})) END AS p10s,
                   CASE WHEN n >= {DENSE_N} OR imp_s IS NULL THEN q90_raw
                        ELSE p50s_raw + (q90_raw - q50_raw) * n / (n + {SHRINK_K}) END AS p90s
            FROM (
                SELECT i.*,
                       (i.n * i.q50_raw + {SHRINK_K} * imp_s) / (i.n + {SHRINK_K}) AS p50s_raw
                FROM imp i
            )
        )
    """).df()

    con.execute("DROP TABLE IF EXISTS t_raw")
    con.execute("DROP TABLE IF EXISTS t_rh")
    con.execute("DROP TABLE IF EXISTS t_ed")

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

    # observed headways: median gap between consecutive bus passages.
    # wait = clamp(headway_p50 / 2, 2, 15) minutes.
    # v1.1: per (route, stop, hour) when samples allow, pooled fallback.
    passages = sorted(CLEAN.glob("passages_*.parquet"))
    if passages:
        pfiles = ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in passages)
        headway = con.execute(f"""
            WITH p AS (
                SELECT route_id, stop_id, tsec
                FROM read_parquet([{pfiles}])
                WHERE tsec IS NOT NULL
            ), s AS (
                SELECT *, lead(tsec) OVER w AS nxt,
                       ((tsec + 19800) // 3600) % 24 AS h_ist_0
                FROM p WINDOW w AS (PARTITION BY route_id, stop_id ORDER BY tsec)
            )
            SELECT route_id, stop_id,
                   quantile_cont(nxt - tsec, 0.5) AS h50_s,
                   COUNT(*) AS n
            FROM s
            WHERE nxt - tsec BETWEEN 120 AND 3600
            GROUP BY 1, 2
        """).df()
        headway_hour = con.execute(f"""
            WITH p AS (
                SELECT route_id, stop_id, tsec
                FROM read_parquet([{pfiles}])
                WHERE tsec IS NOT NULL
            ), s AS (
                SELECT *, lead(tsec) OVER w AS nxt
                FROM p WINDOW w AS (PARTITION BY route_id, stop_id ORDER BY tsec)
            ), g AS (
                SELECT *, ((tsec + 19800) // 3600) % 24 AS h_ist
                FROM s WHERE nxt - tsec BETWEEN 120 AND 3600
            )
            SELECT route_id, stop_id, h_ist,
                   quantile_cont(nxt - tsec, 0.5) AS h50_s,
                   COUNT(*) AS n
            FROM g
            GROUP BY 1, 2, 3 HAVING COUNT(*) >= 4
        """).df()
    else:
        headway = pd.DataFrame()
        headway_hour = pd.DataFrame()
    headway.to_parquet(MODEL / "headway.parquet", index=False)
    headway_hour.to_parquet(MODEL / "headway_hour.parquet", index=False)

    trips = con.execute(f"SELECT COUNT(DISTINCT trip_id) n FROM read_parquet([{files}])").fetchone()[0]
    rows = con.execute(f"SELECT COUNT(*) n FROM read_parquet([{files}])").fetchone()[0]

    edge_hour.to_parquet(MODEL / "edge_hour.parquet", index=False)
    route_hour.to_parquet(MODEL / "route_hour.parquet", index=False)
    route_speed.to_parquet(MODEL / "route_speed.parquet", index=False)
    city_hour.to_parquet(MODEL / "city_hour.parquet", index=False)

    meta = {
        "version": "v1.1-quantiles-shrunk",
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "days": [p.stem.replace("traversals_", "") for p in srcs],
        "n_traversal_rows": int(rows),
        "n_trips_observed": int(trips),
        "n_edge_hour_cells": int(len(edge_hour)),
        "n_routes": int(route_speed.route_id.nunique()),
        "quantiles": ("p10/p50/p90 empirical; cells n>=5; sparse cells "
                      "(n<24) shrunk toward route-hour implied time (K=10)"),
    }
    (MODEL / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[04] model v1.1 trained: {len(edge_hour):,} edge×hour cells, "
          f"{route_speed.route_id.nunique()} routes, {trips:,} observed trips")
    if len(city_hour):
        am = city_hour[city_hour.hour_ist == 9].speed_kmh
        pm = city_hour[city_hour.hour_ist == 21].speed_kmh
        if len(am) and len(pm):
            print(f"[04] sanity: 09:00 speed {am.iloc[0]:.1f} km/h vs "
                  f"21:00 {pm.iloc[0]:.1f} km/h (peak should be slower)")


if __name__ == "__main__":
    main()
