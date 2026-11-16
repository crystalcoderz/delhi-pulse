"""
Stage 4g: observed service headways AND departure gaps from trip labels.

Every GPS trip label is `route_HH_MM[_n]`, i.e. route + scheduled departure.
Two things are derived from the label alone (no GTFS needed):

1. services/hour (existing behaviour): distinct labels per (route, departure
   hour), averaged over available days -> headway = 3600/n -> planner wait.

2. B5 observed gaps: distinct departure minutes per route, ordered through the
   day. The gap to the NEXT departure is assigned to the hour of the EARLIER
   departure — a passenger arriving late in hour h is exposed to the stretch
   into h+1. Same-minute label variants (`route_HH_MM_1`/`_2`) collapse to one
   departure: a re-broadcast variant is not a second bus. No wrap-around gap
   (last departure -> first of the next day): the overnight layover is not an
   in-service wait. Per (route, hour):
     gap_mean_s   mean observed gap [s]
     gap_cv       coefficient of variation (sd/mean; 0.0 when the hour holds
                  exactly one observed gap; NULL when no gap is observed)
     n_gaps       number of gaps behind the statistics
   `wait_mean_s` aliases gap_mean_s: api/app.py B5 applies the
   arrival-theorem correction mean*(1+cv^2)/2 itself, so the stored value is
   the raw mean gap, NOT an adjusted wait.

Output: model/wait_headway_real.parquet
  (route_id, hour_ist, n_services_per_day, headway_s, days,
   gap_mean_s, wait_mean_s, gap_cv, n_gaps)
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
    if not files:
        raise SystemExit("[04g] no pings parquets")
    flist = ", ".join(f"'{f.as_posix()}'" for f in files)

    con = duckdb.connect()
    con.execute("SET threads=4")
    df = con.execute(f"""
        WITH lab AS (
            SELECT route_id, trip_id,
                   TRY_CAST(split_part(trip_id, '_', 2) AS INTEGER) AS dep_h,
                   TRY_CAST(split_part(trip_id, '_', 3) AS INTEGER) AS dep_m,
                   array_length(string_split(trip_id, '_')) AS nparts
            FROM read_parquet([{flist}])
            WHERE trip_id IS NOT NULL AND route_id IS NOT NULL
        ),
        svc AS (
            SELECT route_id, dep_h, COUNT(DISTINCT trip_id) AS n
            FROM lab
            WHERE nparts >= 3 AND dep_h BETWEEN 0 AND 23
            GROUP BY 1, 2
        ),
        deps AS (          -- distinct departure minute-of-day per route
            SELECT DISTINCT route_id, dep_h, dep_h * 60 + dep_m AS tod
            FROM lab
            WHERE nparts >= 3 AND dep_h BETWEEN 0 AND 23
              AND dep_m BETWEEN 0 AND 59
        ),
        gaps AS (
            SELECT route_id, dep_h AS hour_ist,
                   COUNT(*) AS n_gaps,
                   AVG(tod_next - tod) * 60.0 AS gap_mean_s,
                   COALESCE(STDDEV_SAMP(tod_next - tod), 0.0) * 60.0 AS gap_sd_s
            FROM (
                SELECT route_id, dep_h, tod,
                       LEAD(tod) OVER (PARTITION BY route_id ORDER BY tod)
                           AS tod_next
                FROM deps
            )
            WHERE tod_next IS NOT NULL
            GROUP BY 1, 2
        )
        SELECT s.route_id, s.dep_h AS hour_ist, s.n,
               g.n_gaps, g.gap_mean_s, g.gap_sd_s
        FROM svc s
        LEFT JOIN gaps g
               ON g.route_id = s.route_id AND g.hour_ist = s.dep_h
    """).df()
    con.close()

    n_days = len({f.stem.replace("pings_", "") for f in files})
    df["n_services_per_day"] = (df.n / max(1, n_days)).round(2)
    df["headway_s"] = (3600.0 / df.n_services_per_day.clip(lower=0.05)).round(0)
    df["days"] = n_days
    # B5 gap statistics: NULL when the hour has no following departure
    # (single/last departure), 0.0 cv when exactly one gap is observed.
    df["gap_cv"] = (df.gap_sd_s / df.gap_mean_s.where(df.gap_mean_s > 0)) \
        .clip(0.0, 5.0).round(3)
    df["gap_mean_s"] = df.gap_mean_s.round(1)
    df["wait_mean_s"] = df.gap_mean_s        # alias consumed by api/app.py B5
    df["n_gaps"] = df.n_gaps.fillna(0).astype("int64")
    df[["route_id", "hour_ist", "n_services_per_day", "headway_s", "days",
        "gap_mean_s", "wait_mean_s", "gap_cv", "n_gaps"]] \
        .to_parquet(MODEL / "wait_headway_real.parquet", index=False)
    meta = dict(generated=dt.datetime.utcnow().isoformat(), days=n_days,
                n_cells=int(len(df)),
                note="services/day per (route, scheduled-departure hour) from "
                     "trip labels; gap_mean_s/gap_cv = observed consecutive-"
                     "departure gaps (B5), gaps within the service day only")
    (MODEL / "meta_wait.json").write_text(json.dumps(meta))
    top = df.sort_values("n_services_per_day", ascending=False).head(3)
    gstats = df.dropna(subset=["gap_mean_s"])
    if len(gstats):
        print(f"[04g] {len(df):,} (route x hour) headway cells over {n_days} "
              f"days; {len(gstats):,} with observed gaps "
              f"(median gap {gstats.gap_mean_s.median():.0f} s, "
              f"median cv {gstats.gap_cv.median():.2f})")
    else:
        print(f"[04g] {len(df):,} (route x hour) headway cells over {n_days} "
              f"days; no observed gap statistics")
    print(top.to_string(index=False))


if __name__ == "__main__":
    main()
