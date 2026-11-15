"""
Test A: JOURNEY-level replay validation.
Ground truth = observed traversals (route, from_stop -> to_stop, real duration).
For each sample: ask the planner the OD (at stop level, same hour), find the
option riding that route across that edge, compare predicted vs actual ride.

Reports: ride MAE, %-within buckets, and COVERAGE (ration of samples where
the planner even returned a matching option).

With real multi-day traversals: use --train-days/--test-days for a temporal
hold-out (model must be trained without test days).
Usage: python validation/holdout_journeys.py --n 300
"""
import argparse
import functools
import json
import sys
from pathlib import Path
from urllib.parse import quote

print = functools.partial(print, flush=True)

import duckdb
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--days", nargs="*", default=None)
    a = ap.parse_args()

    if a.days:
        files = ", ".join(
            f"'{(CLEAN / f'traversals_{d}.parquet').as_posix()}'" for d in a.days)
        src = f"read_parquet([{files}])"
    else:
        src = f"'{(CLEAN / 'traversals_*.parquet').as_posix()}'"

    con = duckdb.connect()
    df = con.execute(f"""
        SELECT route_id, trip_id, from_stop, to_stop, dist_m, dur_s, hour_ist
        FROM {src}
        WHERE dur_s > 60
        USING SAMPLE {a.n} ROWS (reservoir, {a.seed})
    """).df().dropna()

    results = []
    no_option = no_match = 0
    with httpx.Client(base_url="http://127.0.0.1:8010", timeout=30.0) as c:
        for r in df.itertuples():
            q = (f"/journeys?from=stop:{r.from_stop}&to=stop:{r.to_stop}"
                 f"&depart=2026-10-20T{int(r.hour_ist):02d}:00:00%2B05:30")
            try:
                res = c.get(q).json()
            except Exception as e:
                results.append(dict(error=str(e)))
                continue
            opts = res.get("options") or []
            if not opts:
                no_option += 1
                continue
            want_route = str(r.route_id)
            hit = next((o for o in opts
                        if o.get("route_id") and want_route in str(o["route_id"])
                        and o.get("board") == str(r.from_stop)
                        and o.get("alight") == str(r.to_stop)), None)
            if hit is None:
                hit = next((o for o in opts
                            if o.get("route_id") and want_route in str(o["route_id"])), None)
            if hit is None:
                no_match += 1
                continue
            pred_min = hit["eta_p50_min"]
            # subtract planner's walk+wait extras to isolate the RIDE
            ride_pred = float(hit.get("ride_min") or
                              (pred_min - hit.get("walk_o", 0) - hit.get("walk_d", 0)
                               - hit.get("wait_min", 0)))
            act_min = r.dur_s / 60.0
            results.append(dict(route=want_route, hour=int(r.hour_ist),
                                pred=round(ride_pred, 2), act=round(act_min, 2),
                                err=round(ride_pred - act_min, 2),
                                total_min=pred_min))

    good = [x for x in results if "err" in x]
    if good:
        errs = [x["err"] for x in good]
        n = len(errs)
        mae = sum(abs(e) for e in errs) / n
        w5 = sum(1 for e in errs if abs(e) <= 5) / n
        w10 = sum(1 for e in errs if abs(e) <= 10) / n
        bias = sum(errs) / n
    else:
        mae = w5 = w10 = bias = 0.0
        n = 0
    coverage = len(good) / max(1, len(df))
    report = dict(
        test="journey-replay", n_samples=len(df), n_scored=n,
        no_option=no_option, no_match=no_match,
        ride_mae_min=round(mae, 2), ride_bias_min=round(bias, 2),
        within_5min=round(w5, 3), within_10min=round(w10, 3),
        coverage=round(coverage, 3),
        days=a.days or "all-available(in-sample)",
    )
    (MODEL / "holdout_journeys.json").write_text(json.dumps(report, indent=2))
    (MODEL / "holdout_journey_rows.json").write_text(json.dumps(results[:500]))
    print(json.dumps(report, indent=2))
    print(f"saved -> {MODEL / 'holdout_journeys.json'}")


if __name__ == "__main__":
    main()
