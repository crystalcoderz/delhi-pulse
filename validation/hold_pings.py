"""
Temporal accuracy of the route x hour ping model (leave-one-day-out).

For every (route, hour) observation on day X, predict the cell's n-weighted
mean computed from all OTHER days -> compare. Baseline: predicting the city
mean speed for that hour. Reports the per-route model's lift over naive.

This is the honest, generalization-measured accuracy of the speed layer that
serves /lookup, /geo, replay, and the planner's ride times (pre-GTFS).
Usage: python validation/hold_pings.py
"""
import functools
import json
import sys
from pathlib import Path

print = functools.partial(print, flush=True)

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402

MIN_PAIRS_CELL = 12   # observed pairs in the test day's cell


def main():
    files = sorted(CLEAN.glob("speeds_*.parquet"))
    # one row per (day, route, hour, speed, n)
    df = pd.concat(
        [pd.read_parquet(p).assign(day=p.stem.replace("speeds_", ""))
         for p in files], ignore_index=True)
    df["wn"] = df.speed_kmh * df.n
    days = sorted(df.day.unique())
    if len(days) < 3:
        raise SystemExit("[H] need >=3 days of speeds for leave-one-out")

    rows = []
    for d in days:
        test = df[df.day == d]
        rest = df[df.day != d]
        rest_g = rest.groupby(["route_id", "hour_ist"]).agg(
            wn=("wn", "sum"), n=("n", "sum")).reset_index()
        rest_g["pred"] = rest_g.wn / rest_g.n
        city_hr = rest.groupby("hour_ist").agg(
            wn=("wn", "sum"), n=("n", "sum")).reset_index()
        city_hr["city_pred"] = city_hr.wn / city_hr.n
        m = test.merge(rest_g, on=["route_id", "hour_ist"], how="inner") \
                 .merge(city_hr[["hour_ist", "city_pred"]], on="hour_ist")
        m = m[m.n_x >= MIN_PAIRS_CELL]
        m["err_model"] = m.pred - m.speed_kmh
        m["err_city"] = m.city_pred - m.speed_kmh
        rows.append(m[["route_id", "hour_ist", "day", "speed_kmh", "pred",
                       "city_pred", "err_model", "err_city"]])
    r = pd.concat(rows, ignore_index=True)

    mae_m = float(np.mean(np.abs(r.err_model)))
    mae_c = float(np.mean(np.abs(r.err_city)))
    mape_m = float(np.mean(np.abs(r.err_model) / r.speed_kmh.clip(lower=1)) * 100)
    within10 = float(np.mean(np.abs(r.err_model) <= 10))
    within5 = float(np.mean(np.abs(r.err_model) <= 5))

    rep = dict(
        test="leave-one-day-out route x hour speeds",
        days=days, n_cells_evald=int(len(r)),
        mae_model_kmh=round(mae_m, 2), mae_city_baseline_kmh=round(mae_c, 2),
        lift_vs_baseline=round((1 - mae_m / mae_c) * 100, 1),
        mape_model_pct=round(mape_m, 1),
        within_5kmh=round(within5, 3), within_10kmh=round(within10, 3),
    )
    (MODEL / "holdout_pings.json").write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
    # per-day breakdown for the report
    per_day = r.groupby("day").apply(lambda x: pd.Series({
        "mae_model": float(np.mean(np.abs(x.err_model))),
        "mae_city": float(np.mean(np.abs(x.err_city))),
        "n": int(len(x))}), include_groups=False).reset_index()
    print(per_day.to_string(index=False))


if __name__ == "__main__":
    main()
