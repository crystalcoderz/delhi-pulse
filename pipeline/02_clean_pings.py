"""
Stage 2: clean a day of raw GPS pings -> tidy parquet.
  - dedupe (vehicle_id, timestamp)
  - drop rows outside the Delhi bbox, junk lat/lng text, null trip_id
  - drop teleports (implied speed > MAX_IMPLIED_KMH AND distance > JUMP_MIN_M)
  - convert to IST, add hour/dow
Output: data/clean/pings_YYYY_MM_DD.parquet
Usage:
  python pipeline/02_clean_pings.py --day 2026_10_15
"""
import argparse
import sqlite3
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
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='.ducktmp'")

    con.execute("""
        CREATE TABLE raw(vehicle_id VARCHAR, lat DOUBLE, lng DOUBLE, speed DOUBLE,
                         route_id BIGINT, trip_id VARCHAR, ts_utc BIGINT)
    """)

    src = sqlite3.connect(db)
    cur = src.cursor()
    cur.execute("""SELECT vehicle_id,
                          CAST(lat AS DOUBLE), CAST(lng AS DOUBLE),
                          speed, CAST(route_id AS BIGINT),
                          trip_id,
                          CAST("timestamp" AS BIGINT)
                   FROM vehicle_feed""")
    n_in = 0
    INS = "INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?)"
    while True:
        rows = cur.fetchmany(500_000)
        if not rows:
            break
        con.executemany(INS, rows)
        n_in += len(rows)
    src.close()
    print(f"[02] loaded {n_in:,} rows from {db.name}")

    lat0, lng0, lat1, lng1 = BBOX
    con.execute(f"""
        CREATE TABLE clean AS
        WITH ded AS (
            SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY trip_id, vehicle_id, ts_utc ORDER BY speed DESC
                   ) AS rn
            FROM raw
            WHERE lat BETWEEN {lat0} AND {lat1}
              AND lng BETWEEN {lng0} AND {lng1}
              AND trip_id IS NOT NULL
        ),
        base AS (
            SELECT vehicle_id, trip_id, route_id, lat, lng, speed,
                   ts_utc, ts_utc + {IST_OFFSET_S} AS ts_ist
            FROM ded WHERE rn = 1
        ),
        seq AS (
            SELECT *,
                   lead(lat)  OVER w AS nlat,
                   lead(lng)  OVER w AS nlng,
                   lead(ts_utc) OVER w AS nts
            FROM base
            WINDOW w AS (PARTITION BY trip_id, vehicle_id ORDER BY ts_utc)
        )
        SELECT trip_id, vehicle_id, route_id, lat, lng, speed, ts_utc,
               to_timestamp(ts_utc + {IST_OFFSET_S}) AS dt_ist,
               hour(to_timestamp(ts_utc + {IST_OFFSET_S})) AS hour_ist,
               isodow(to_timestamp(ts_utc + {IST_OFFSET_S})) AS dow_ist,
               nlat, nlng, nts
        FROM seq
    """)

    # teleport filter: only kill the physically impossible AND far jumps
    con.execute(f"""
        CREATE TABLE clean2 AS
        SELECT trip_id, vehicle_id, route_id, lat, lng, speed, ts_utc,
               dt_ist, hour_ist, dow_ist
        FROM (
            SELECT *,
                   CASE WHEN nts > ts_utc THEN
                        sqrt( power((nlng-lng)*111320*cos(radians((nlat+lat)/2)),2)
                            + power((nlat-lat)*110540,2) )
                        / (nts - ts_utc) * 3.6
                   ELSE NULL END AS kmh_next
            FROM clean
        ) t
        WHERE kmh_next IS NULL
           OR (kmh_next <= {MAX_IMPLIED_KMH}
               AND sqrt( power((nlng-lng)*111320*cos(radians((nlat+lat)/2)),2)
                       + power((nlat-lat)*110540,2) ) <= {JUMP_MIN_M})
           OR kmh_next <= {MAX_IMPLIED_KMH}
    """)

    # drop tiny trips, then write
    con.execute(f"""
        COPY (
            SELECT c.* FROM clean2 c
            JOIN (SELECT trip_id, vehicle_id FROM clean2
                  GROUP BY trip_id, vehicle_id HAVING COUNT(*) >= 6) k
              ON c.trip_id = k.trip_id AND c.vehicle_id = k.vehicle_id
        ) TO '{str(out).replace(chr(92), "/")}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n_out = con.execute("SELECT COUNT(*) FROM clean2").fetchone()[0]
    trips = con.execute("SELECT COUNT(DISTINCT trip_id) FROM clean2").fetchone()[0]
    con.close()
    print(f"[02] kept {n_out:,}/{n_in:,} rows ({100*n_out/max(1,n_in):.1f}%), "
          f"{trips:,} trips -> {out.name}")


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
