"""
Stage 2: clean a day of raw GPS pings -> tidy parquet.
  - dedupe (trip, vehicle, ts)
  - drop rows outside the Delhi bbox, junk lat/lng, null trip_id
  - drop teleports (implied speed > MAX_IMPLIED_KMH)
  - IST hour/dow
Direct DuckDB-over-SQLite scan: no Python row loops, parallel columnar.
Output: data/clean/pings_YYYY_MM_DD.parquet
Usage:
  python pipeline/02_clean_pings.py --day 2026_10_15
"""
import argparse
import os
from pathlib import Path

import duckdb

from common import (ensure_dirs, RAW, CLEAN, BBOX, IST_OFFSET_S,
                    MAX_IMPLIED_KMH, JUMP_MIN_M)


def day_db(day: str) -> Path:
    p = RAW / f"bus_movements_{day}.db"
    if not p.exists():
        raise SystemExit(f"[02] missing {p} — run 01_extract_day.py first")
    return p


def clean(day: str):
    ensure_dirs()
    db = day_db(day)
    out = CLEAN / f"pings_{day}.parquet"

    mem = os.environ.get("DUCKDB_MEM", "4GB")
    thr = os.environ.get("DUCKDB_THREADS", "4")
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{mem}'")
    con.execute(f"SET threads={thr}")
    con.execute("SET temp_directory='.ducktmp'")
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{db.as_posix()}' AS src (TYPE sqlite)")

    n_in = con.execute("SELECT COUNT(*) FROM src.vehicle_feed").fetchone()[0]

    lat0, lng0, lat1, lng1 = BBOX
    n_out = con.execute(f"""
        COPY (
            WITH base AS (
                SELECT trip_id, vehicle_id,
                       TRY_CAST(route_id AS BIGINT) AS route_id,
                       TRY_CAST(lat AS DOUBLE) AS lat,
                       TRY_CAST(lng AS DOUBLE) AS lng,
                       speed,
                       TRY_CAST("timestamp" AS BIGINT) AS ts_utc
                FROM src.vehicle_feed
                WHERE lat IS NOT NULL AND "timestamp" IS NOT NULL
                  AND trip_id IS NOT NULL
            ),
 inbox AS (
                SELECT * FROM base
                WHERE lat BETWEEN {lat0} AND {lat1}
                  AND lng BETWEEN {lng0} AND {lng1}
            ),
            ded AS (
                SELECT *, ROW_NUMBER() OVER (
                           PARTITION BY trip_id, vehicle_id, ts_utc
                           ORDER BY speed DESC NULLS LAST) AS rn
                FROM inbox
            ),
            seq AS (
                SELECT trip_id, vehicle_id, route_id, lat, lng, speed, ts_utc,
                       lead(lat) OVER w AS nlat,
                       lead(lng) OVER w AS nlng,
                       lead(ts_utc) OVER w AS nts
                FROM ded WHERE rn = 1
                WINDOW w AS (PARTITION BY trip_id, vehicle_id ORDER BY ts_utc)
            )
            SELECT c.trip_id, c.vehicle_id, c.route_id, c.lat, c.lng, c.speed,
                   c.ts_utc,
                   to_timestamp(c.ts_utc + {IST_OFFSET_S}) AS dt_ist,
                   hour(to_timestamp(c.ts_utc + {IST_OFFSET_S})) AS hour_ist,
                   isodow(to_timestamp(c.ts_utc + {IST_OFFSET_S})) AS dow_ist
            FROM seq c
            WHERE (c.nts IS NULL OR c.nts <= c.ts_utc)          -- last row / dup ts
               OR (c.nts > c.ts_utc AND
                   sqrt( power((c.nlng-c.lng)*111320.0*cos(radians((c.nlat+c.lat)/2.0)),2)
                       + power((c.nlat-c.lat)*110540.0,2) )
                   <= GREATEST({JUMP_MIN_M}, (c.nts-c.ts_utc) * {MAX_IMPLIED_KMH} / 3.6))
        ) TO '{out.as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """).fetchone()

    n_out = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
    trips = con.execute(
        f"SELECT COUNT(DISTINCT trip_id) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
    con.close()
    print(f"[02] kept {n_out:,}/{n_in:,} rows ({100*n_out/max(1,n_in):.1f}%), "
          f"{trips:,} trips -> {out.name} ({out.stat().st_size/1e6:.0f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026_10_15")
    ap.add_argument("--all", action="store_true", help="process every db in data/raw")
    a = ap.parse_args()
    if a.all:
        for p in sorted(RAW.glob("bus_movements_*.db")):
            d = p.stem.replace("bus_movements_", "")
            if not (CLEAN / f"pings_{d}.parquet").exists():
                clean(d)
            else:
                print(f"[02] skip {d} (exists)")
    else:
        clean(a.day)
