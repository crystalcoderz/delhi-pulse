"""
Stage 4f: route x hour observed speeds derived DIRECTLY from ping pairs
(implied speed between consecutive pings of the same trip) — no GTFS needed.

Feeds: _day_speeds() fallback, /geo pulse overlay, replay mode for real days.
Output: data/clean/speeds_YYYY_MM_DD.parquet  (route_id, hour_ist, speed_kmh, n)
Usage:
  python pipeline/04f_pings_speeds.py --day 2026_10_13
"""
import argparse
import os
from pathlib import Path

import duckdb

from common import ensure_dirs, CLEAN, IST_OFFSET_S, hour_ist_expr, dow_ist_expr

MAX_PAIR_KMH = 90.0   # implied speeds above this are GPS noise
MIN_PAIR_M = 15.0     # very short pairs are jitter
MIN_PAIR_KMH = 3.0     # sub-walking-pace pairs are depot creep, not traffic


def main(day: str):
    ensure_dirs()
    src = CLEAN / f"pings_{day}.parquet"
    if not src.exists():
        raise SystemExit(f"[04f] missing {src}")
    out = CLEAN / f"speeds_{day}.parquet"

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '4GB')}'")
    con.execute(f"SET threads={os.environ.get('DUCKDB_THREADS', '4')}")

    df = con.execute(f"""
        WITH seq AS (
            SELECT route_id, lat, lng, ts_utc,
                   {hour_ist_expr("ts_utc")} AS hour_ist,
                   {dow_ist_expr("ts_utc")} AS dow_ist,
                   lead(lat) OVER w AS nlat,
                   lead(lng) OVER w AS nlng,
                   lead(ts_utc) OVER w AS nts
            FROM read_parquet('{src.as_posix()}')
            WHERE route_id IS NOT NULL
            WINDOW w AS (PARTITION BY trip_id, vehicle_id ORDER BY ts_utc)
        ), pairs AS (
            SELECT route_id, hour_ist, dow_ist,
                   sqrt( power((nlng-lng)*111320.0*cos(radians((nlat+lat)/2.0)),2)
                       + power((nlat-lat)*110540.0,2) ) AS dist_m,
                   nts - ts_utc AS dt_s
            FROM seq
            WHERE nts > ts_utc AND nts - ts_utc <= 120
        )
        SELECT route_id, dow_ist, hour_ist,
               SUM(dist_m) / NULLIF(SUM(dt_s),0) * 3.6 AS speed_kmh,
               COUNT(*) AS n
        FROM pairs
        WHERE dist_m > {MIN_PAIR_M}
          AND dist_m / dt_s * 3.6 BETWEEN {MIN_PAIR_KMH} AND {MAX_PAIR_KMH}
        GROUP BY 1, 2, 3
    """).df()

    # trip-weighted route aggregate (for coarse replay fallback)
    # depot creep filtered the same way as cells
    df2 = con.execute(f"""
        WITH seq AS (
            SELECT route_id, lat, lng, ts_utc,
                   lead(lat) OVER w AS nlat,
                   lead(lng) OVER w AS nlng,
                   lead(ts_utc) OVER w AS nts
            FROM read_parquet('{src.as_posix()}')
            WHERE route_id IS NOT NULL
            WINDOW w AS (PARTITION BY trip_id, vehicle_id ORDER BY ts_utc)
        ), p AS (
            SELECT route_id,
                   sqrt(power((nlng-lng)*111320.0*cos(radians((nlat+lat)/2.0)),2)
                        + power((nlat-lat)*110540.0,2)) AS d,
                   nts - ts_utc AS dt
            FROM seq WHERE nts > ts_utc AND nts - ts_utc <= 120
        ), kept AS (
            SELECT * FROM p
            WHERE d > {MIN_PAIR_M} AND d / dt * 3.6 BETWEEN {MIN_PAIR_KMH} AND {MAX_PAIR_KMH}
        )
        SELECT route_id,
               SUM(d) AS dist_sum_m,
               SUM(dt) AS dt_sum_s
        FROM kept
        GROUP BY 1
    """).df()
    con.close()

    df.to_parquet(out, index=False)
    df2.to_parquet(CLEAN / f"speedsagg_{day}.parquet", index=False)
    tot_routes = df.route_id.nunique()
    print(f"[04f] {day}: {len(df):,} route x hour cells across {tot_routes} routes "
          f"-> {out.name}")
    if len(df):
        r9 = df[df.hour_ist == 9].speed_kmh
        r21 = df[df.hour_ist == 21].speed_kmh
        if len(r9) and len(r21):
            print(f"[04f] {day}: 09:00 median {r9.median():.1f} km/h vs "
                  f"21:00 median {r21.median():.1f} km/h")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026_10_15")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    days = [a.day]
    if a.all:
        days = [p.stem.replace("pings_", "")
                for p in sorted(CLEAN.glob("pings_*.parquet"))]
    for d in days:
        main(d)
