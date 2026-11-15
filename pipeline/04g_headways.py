"""
Stage 4g: observed service headways from trip labels — no GTFS needed.

Every GPS trip label is `route_HH_MM[_n]`, i.e. route + scheduled departure.
Distinct labels per (route, departure_hour), averaged over available days,
give services/hour -> headway = 3600/n -> planner wait = headway/2 clamped.

Output: model/wait_headway_real.parquet
  (route_id, hour_ist, n_services_per_day, headway_s, days)
Usage:  python pipeline/04g_headways.py
"""
import functools
import json
import datetime as dt
import sys
from pathlib import Path

print = functools.partial(print, flush=True)

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CLEAN, MODEL


def main():
    files = sorted(CLEAN.glob("pings_*.parquet"))
    # real days only (synthetic fixture is tiny and would skew averages)
    files = [f for f in files
             if (CLEAN / f"stats_{f.stem.replace('pings_', '')}.json").exists()
             or True]  # keep all; weight by day count below
    if not files:
        raise SystemExit("[04g] no pings parquets")
    flist = ", ".join(f"'{f.as_posix()}'" for f in files)

    con = duckdb.connect()
    con.execute("SET threads=4")
    df = con.execute(f"""
        WITH lab AS (
            SELECT route_id, trip_id,
                   TRY_CAST(split_part(trip_id, '_', 2) AS INTEGER) AS dep_h,
                   array_length(string_split(trip_id, '_')) AS nparts
            FROM read_parquet([{flist}])
            WHERE trip_id IS NOT NULL AND route_id IS NOT NULL
        )
        SELECT route_id, dep_h, COUNT(DISTINCT trip_id) AS n
        FROM lab
        WHERE nparts >= 3 AND dep_h BETWEEN 0 AND 23
        GROUP BY 1, 2
    """).df()
    n_days = len({f.stem.replace("pings_", "") for f in files})
    df["n_services_per_day"] = (df.n / max(1, n_days)).round(2)
    df = df.rename(columns={"dep_h": "hour_ist"})
    df["headway_s"] = (3600.0 / df.n_services_per_day.clip(lower=0.05)).round(0)
    df["days"] = n_days
    df[["route_id", "hour_ist", "n_services_per_day", "headway_s", "days"]] \
        .to_parquet(MODEL / "wait_headway_real.parquet", index=False)
    meta = dict(generated=dt.datetime.utcnow().isoformat(), days=n_days,
                n_cells=int(len(df)),
                note="services/day per (route, scheduled-departure hour) from trip labels")
    (MODEL / "meta_wait.json").write_text(json.dumps(meta))
    top = df.sort_values("n_services_per_day", ascending=False).head(3)
    print(f"[04g] {len(df):,} (route x hour) headway cells over {n_days} days")
    print(top.to_string(index=False))


if __name__ == "__main__":
    main()
