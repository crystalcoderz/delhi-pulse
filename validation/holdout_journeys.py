"""Journey-level holdout with a strict time split (F1 + F2 + B7 calibration).

Rebuilds the validation the brief deserves:
  * REAL journeys: one vehicle-trip's traversals chained across 5-40 stop
    hops — not single stop-to-next-stop hops.
  * STRICT SPLIT: model trained on 2026-10-01..24, scored on 2026-10-25..31.
  * NO LEAKAGE: predictions come only from train-window statistics; when an
    edge cell is missing, the fallback is the TRAIN-window route-hour speed
    applied to the leg's physical distance (geometry, never durations).
  * SCALE: mean ride time reported beside the MAE.
  * BREAKDOWNS: error by hour band and by journey length band.

The model under test mirrors the deployed v1.1: edge p50 with n>=5 floor
and route-hour-implied shrinkage (K=10), traversal route-hour fallback.

Output: model/holdout_journeys.json + console summary.
"""
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

print_ = lambda *a: print(*a, flush=True)  # noqa: E731
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402

TRAIN_END = "2026-10-24"
TEST_START = "2026-10-25"
MAX_JOURNEYS = 6000
HOUR_BANDS = [(0, 6), (6, 11), (11, 17), (17, 21), (21, 24)]
LEN_BANDS = [(5, 10), (10, 21), (21, 41)]   # inclusive lo, exclusive hi; labels show lo-(hi-1)


def day_of_file(name):
    p = name.replace("traversals_", "").replace(".parquet", "")
    return f"{p[:4]}-{p[5:7]}-{p[8:10]}"


def sql_files(day_from, day_to):
    fs = [p for p in sorted(CLEAN.glob("traversals_2026_10_*.parquet"))
          if day_from <= day_of_file(p.name) <= day_to]
    assert fs, f"no traversal files in {day_from}..{day_to}"
    return ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in fs)


def run():
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET preserve_insertion_order=false")

    train_f = sql_files("2026-10-01", TRAIN_END)
    test_f = sql_files(TEST_START, "2026-10-31")
    print_("[j] train: Oct 01-24 | test: Oct 25-31")

    # ---- train-window model (mirrors deployed v1.1 logic) ----
    con.execute(f"""
        CREATE OR REPLACE TABLE t_edge AS
        WITH raw AS (
            SELECT route_id, from_stop, to_stop, hour_ist,
                   quantile_cont(dur_s, 0.10) AS q10,
                   quantile_cont(dur_s, 0.50) AS q50,
                   quantile_cont(dur_s, 0.90) AS q90,
                   COUNT(*) AS n,
                   median(dist_m) AS dm
            FROM read_parquet([{train_f}])
            GROUP BY 1,2,3,4 HAVING COUNT(*) >= 5
        ), rh AS (
            SELECT route_id, hour_ist,
                   SUM(dur_s) AS rdur, SUM(dist_m) AS rdist
            FROM read_parquet([{train_f}])
            GROUP BY 1,2
        ), sh AS (
            SELECT c.*, r.rdur, r.rdist,
                   CASE WHEN r.rdist > 0
                        THEN c.dm * r.rdur / r.rdist END AS imp_s
            FROM raw c JOIN rh r USING (route_id, hour_ist)
        )
        SELECT route_id, from_stop, to_stop, hour_ist,
               CASE WHEN n >= 24 OR imp_s IS NULL THEN q50
                    ELSE (n * q50 + 10 * imp_s) / (n + 10) END AS p50,
               CASE WHEN n >= 24 OR imp_s IS NULL THEN GREATEST(q10, 0.4 * q50)
                    ELSE GREATEST(0.4 * q50,
                         (n * q50 + 10 * imp_s) / (n + 10)
                         - (q50 - q10) * n / (n + 10)) END AS p10,
               CASE WHEN n >= 24 OR imp_s IS NULL THEN q90
                    ELSE (n * q50 + 10 * imp_s) / (n + 10)
                         + (q90 - q50) * n / (n + 10) END AS p90,
               n
        FROM sh
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE t_rh AS
        SELECT route_id, hour_ist,
               SUM(dist_m) / NULLIF(SUM(dur_s), 0) * 3.6 AS sp
        FROM read_parquet([{train_f}])
        GROUP BY 1,2
    """)
    n_edge = con.execute("SELECT COUNT(*) FROM t_edge").fetchall()[0][0]
    print_(f"[j] train edge cells: {n_edge:,}")

    # ---- assemble test journeys: one real ride, not a label ----
    # (route_id, trip_id) is a runtime label reused across days/vehicles
    # (14.6% of test-week labels appear on 2-8 days). One journey = one
    # (day, label) chain split at >600 s ride gaps, 5-40 hops. Verified by
    # the leakage audit: naive grouping yielded week-long "rides".
    journeys = con.execute(f"""
        WITH t AS (
            SELECT route_id, trip_id, from_stop, to_stop, dist_m, dur_s,
                   tsec, hour_ist,
                   (tsec + 19800) // 86400 AS d_ist
            FROM read_parquet([{test_f}])
        ), g AS (
            SELECT *,
                   SUM(CASE WHEN tsec - LAG(tsec) OVER w > 600 THEN 1
                            ELSE 0 END) OVER w AS seg
            FROM t WINDOW w AS (PARTITION BY route_id, trip_id, d_ist
                               ORDER BY tsec)
        )
        SELECT route_id, trip_id, d_ist, seg,
               COUNT(*) AS hops,
               MAX(tsec) - MIN(tsec) AS ride_s,
               MIN(hour_ist) AS h0,
               SUM(dist_m) AS dist_m,
               MIN(tsec) AS t0
        FROM g
        GROUP BY 1,2,3,4
        HAVING COUNT(*) BETWEEN 5 AND 40
        ORDER BY RANDOM()
        LIMIT {MAX_JOURNEYS}
    """).df()
    print_(f"[j] test journeys sampled: {len(journeys):,} "
           f"(day+gap segmented)")
    con.register("jn", journeys)

    # ---- score: predict each leg from TRAIN-window stats only; each leg
    # is timed at its own IST hour (mirrors the deployed per-leg hour) ----
    scored = con.execute(f"""
        WITH legs AS (
            SELECT tr.route_id, tr.trip_id,
                   (tr.tsec + 19800) // 86400 AS d_ist,
                   tr.from_stop, tr.to_stop, tr.dist_m, tr.hour_ist,
                   jn.hops, jn.seg
            FROM read_parquet([{test_f}]) tr
            JOIN jn ON jn.trip_id = tr.trip_id AND jn.route_id = tr.route_id
        ), per_leg AS (
            SELECT l.route_id, l.trip_id, l.d_ist, l.seg, l.hops,
                   COALESCE(e.p50, l.dist_m * 3.6 / NULLIF(r.sp, 0))
                       AS leg_pred_s,
                   CASE WHEN e.p50 IS NULL THEN 1 ELSE 0 END AS fb
            FROM legs l
            LEFT JOIN t_edge e
              ON e.route_id = l.route_id AND e.from_stop = l.from_stop
             AND e.to_stop = l.to_stop AND e.hour_ist = l.hour_ist
            LEFT JOIN t_rh r
              ON r.route_id = l.route_id AND r.hour_ist = l.hour_ist
        )
        SELECT route_id, trip_id, d_ist, seg, hops,
               SUM(leg_pred_s) AS pred_s,
               SUM(fb) AS n_fallback,
               COUNT(*) AS n_legs
        FROM per_leg
        GROUP BY 1,2,3,4,5,6
        HAVING COUNT(*) = hops      -- score journeys whole or not at all
    """).df()
    print_(f"[j] journeys scored: {len(scored):,}")
    con.close()

    df = scored.merge(journeys[["route_id", "trip_id", "d_ist", "seg",
                                "ride_s", "dist_m"]],
                     on=["route_id", "trip_id", "d_ist", "seg"])
    df["err_min"] = (df.pred_s - df.ride_s) / 60.0
    df["abs_min"] = df.err_min.abs()

    def band(v, bins):
        for lo, hi in bins:
            if lo <= v < hi:
                return f"{lo}-{hi-1}"
        return "other"

    df["hband"] = df.h0.map(lambda h: band(int(h), HOUR_BANDS))
    df["lband"] = df.hops.map(lambda h: band(int(h), LEN_BANDS))

    out = {
        "test": "journey-level holdout, train Oct 01-24, score Oct 25-31",
        "n_journeys": int(len(df)),
        "mean_ride_min": round(df.ride_s.mean() / 60.0, 1),
        "mean_pred_min": round(df.pred_s.mean() / 60.0, 1),
        "mae_min": round(df.abs_min.mean(), 2),
        "bias_min": round(df.err_min.mean(), 2),
        "within_5_min": round((df.abs_min <= 5).mean(), 3),
        "within_10_min": round((df.abs_min <= 10).mean(), 3),
        "fallback_leg_share": round(float(df.n_fallback.sum())
                                    / max(1, int(df.n_legs.sum())), 3),
        "legs_per_journey": round(float(df.n_legs.mean()), 1),
        "by_hour_band": {},
        "by_length_band": {},
    }
    for b, g in df.groupby("hband"):
        out["by_hour_band"][b] = {
            "n": int(len(g)), "mae_min": round(g.abs_min.mean(), 2),
            "mean_ride_min": round(g.ride_s.mean() / 60.0, 1)}
    for b, g in df.groupby("lband"):
        out["by_length_band"][b] = {
            "n": int(len(g)), "mae_min": round(g.abs_min.mean(), 2),
            "mean_ride_min": round(g.ride_s.mean() / 60.0, 1)}

    (MODEL / "holdout_journeys.json").write_text(json.dumps(out, indent=2))
    print_(f"[j] MAE {out['mae_min']} min on mean ride "
           f"{out['mean_ride_min']} min ({out['n_journeys']} journeys; "
           f"{out['fallback_leg_share']*100:.0f}% legs on fallback)")
    for b in sorted(out["by_hour_band"]):
        d_ = out["by_hour_band"][b]
        print_(f"    hour {b:>6}: n={d_['n']:>4} mae={d_['mae_min']:>6}"
               f" mean ride={d_['mean_ride_min']} min")
    for b in sorted(out["by_length_band"]):
        d_ = out["by_length_band"][b]
        print_(f"    hops {b:>6}: n={d_['n']:>4} mae={d_['mae_min']:>6}"
               f" mean ride={d_['mean_ride_min']} min")


if __name__ == "__main__":
    run()
