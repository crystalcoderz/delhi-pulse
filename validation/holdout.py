"""
Validation harness — edge-level replay (Test B core, feeds Test A later).

Method: sample real observed edge traversals -> ask the planner's own
`leg_seconds` what it would have predicted for that (route, edge, hour)
-> report error distribution. With multi-day data, pass --train-days to
evaluate on a temporal hold-out (model must be trained without test days).

Usage:
  python pipeline/holdout.py                     # all traversals, in-sample
  python pipeline/holdout.py --n 5000            # sample size
  python pipeline/holdout.py --test-days 2026_10_25 ...
"""
import argparse
import functools
import json
import sys
from pathlib import Path

print = functools.partial(print, flush=True)

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402
from api.app import leg_seconds  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--test-days", nargs="*", default=None,
                    help="only evaluate traversals from these days (must be excluded from training)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    con = duckdb.connect()
    where = ""
    if a.test_days:
        dlist = ", ".join(f"'traversals_{d}.parquet'" for d in a.test_days)
        where = f"WHERE filename LIKE ANY ({dlist})" if False else ""
        files = ", ".join(f"'{(CLEAN / f'traversals_{d}.parquet').as_posix()}'" for d in a.test_days)
        src = f"read_parquet([{files}])"
    else:
        src = f"'{ (CLEAN / 'traversals_*.parquet').as_posix() }'"

    df = con.execute(f"""
        SELECT route_id, from_stop, to_stop, hour_ist, dist_m, dur_s
        FROM {src}
        USING SAMPLE {a.n} ROWS (reservoir, {a.seed})
    """).df().dropna()
    n0 = len(df)
    errs = []
    rows = []
    for r in df.itertuples():
        pred = leg_seconds(str(r.route_id), str(r.from_stop), str(r.to_stop),
                           float(r.dist_m), int(r.hour_ist))
        act = float(r.dur_s)
        if act <= 0 or pred <= 0:
            continue
        errs.append(pred - act)
        rows.append(dict(route=str(r.route_id), edge=f"{r.from_stop}->{r.to_stop}",
                         hour=int(r.hour_ist), pred_min=round(pred / 60, 2),
                         act_min=round(act / 60, 2), err_min=round((pred - act) / 60, 2)))
    import numpy as np
    e = np.array(errs) / 60.0
    acts = df.dur_s.values[:len(errs)] / 60.0
    preds = acts + e
    mae = float(np.mean(np.abs(e)))
    within5 = float(np.mean(np.abs(e) <= 5))
    within10 = float(np.mean(np.abs(e) <= 10))
    bias = float(np.mean(e))
    # naive baseline: city-hour speed x distance (no per-edge model)
    naive_errs = []
    report = dict(
        n_eval=int(len(e)), n_skipped=int(n0 - len(e)),
        mae_min=round(mae, 2), bias_min=round(bias, 2),
        within_5min=round(within5, 3), within_10min=round(within10, 3),
        test_days=a.test_days or "in-sample",
    )
    out = MODEL / f"holdout_report.json"
    out.write_text(json.dumps(report, indent=2))
    (MODEL / "holdout_rows.json").write_text(json.dumps(rows[:2000]))
    print(json.dumps(report, indent=2))
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
