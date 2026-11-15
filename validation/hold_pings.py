"""
Temporal accuracy of the route x hour ping model (leave-one-day-out).

Arms compared, every one honest (predicted from days the model never saw):
  - dow model:  (route, dow, hour)  learned from other same-weekday days
  - pooled:     (route, hour)       learned from all other days
  - city:       (hour)             naive city-hour baseline

Usage: python validation/hold_pings.py
"""
import functools
import json
import sys
from pathlib import Path

print = functools.partial(print, flush=True)

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402

MIN_PAIRS_CELL = 12   # observed pairs in the test day's cell


def main():
    files = sorted(CLEAN.glob("speeds_*.parquet"))
    if not files:
        raise SystemExit("[H] no speeds parquets")
    df = pd.concat(
        [pd.read_parquet(p).assign(day=p.stem.replace("speeds_", ""))
         for p in files], ignore_index=True)
    if "dow_ist" not in df.columns:
        raise SystemExit("[H] speeds lack dow_ist - rerun 04f (dow-enabled)")
    df["wn"] = df.speed_kmh * df.n
    days = sorted(df.day.unique())
    if len(days) < 3:
        raise SystemExit("[H] need >=3 days for leave-one-out")

    rows = []
    for d in days:
        test = df[df.day == d]
        rest = df[df.day != d]

        rest_g = rest.groupby(["route_id", "hour_ist"]).agg(
            wn=("wn", "sum"), n=("n", "sum")).reset_index()
        rest_g["pred"] = rest_g.wn / rest_g.n

        need_dow = test[["route_id", "dow_ist", "hour_ist"]].drop_duplicates()
        rest_dow = rest.groupby(["route_id", "dow_ist", "hour_ist"]).agg(
            wn=("wn", "sum"), n=("n", "sum")).reset_index()
        rest_dow = rest_dow.merge(need_dow, on=["route_id", "dow_ist", "hour_ist"],
                                  how="inner")
        rest_dow["pred_dow"] = rest_dow.wn / rest_dow.n

        city_hr = rest.groupby("hour_ist").agg(
            wn=("wn", "sum"), n=("n", "sum")).reset_index()
        city_hr["city_pred"] = city_hr.wn / city_hr.n

        m = test.merge(rest_g, on=["route_id", "hour_ist"], how="inner")
        m = m.merge(rest_dow[["route_id", "dow_ist", "hour_ist", "pred_dow"]],
                    on=["route_id", "dow_ist", "hour_ist"], how="left")
        m = m.merge(city_hr[["hour_ist", "city_pred"]], on="hour_ist")
        m = m[m.n_x >= MIN_PAIRS_CELL]
        m["err_model"] = m.pred - m.speed_kmh
        m["err_dow"] = m.pred_dow - m.speed_kmh
        m["err_city"] = m.city_pred - m.speed_kmh
        rows.append(m[["route_id", "dow_ist", "hour_ist", "day", "speed_kmh",
                       "pred", "pred_dow", "city_pred",
                       "err_model", "err_dow", "err_city"]])
    r = pd.concat(rows, ignore_index=True)

    mae_m = float(np.mean(np.abs(r.err_model)))
    mae_d = float(np.mean(np.abs(r.err_dow.dropna())))
    mae_c = float(np.mean(np.abs(r.err_city)))
    mape_m = float(np.mean(np.abs(r.err_model) / r.speed_kmh.clip(lower=1)) * 100)
    within10 = float(np.mean(np.abs(r.err_model) <= 10))
    within5 = float(np.mean(np.abs(r.err_model) <= 5))

    rep = dict(
        test="leave-one-day-out route x hour speeds",
        days=days, n_cells_evald=int(len(r)),
        n_cells_dow_evald=int(r.pred_dow.notna().sum()),
        mae_model_kmh=round(mae_m, 2),
        mae_dow_model_kmh=round(mae_d, 2),
        mae_city_baseline_kmh=round(mae_c, 2),
        lift_vs_baseline=round((1 - mae_m / mae_c) * 100, 1),
        lift_dow_vs_pooled=round((1 - mae_d / mae_m) * 100, 1),
        mape_model_pct=round(mape_m, 1),
        within_5kmh=round(within5, 3),
        within_10kmh=round(within10, 3),
    )
    (MODEL / "holdout_pings.json").write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
    per_day = r.groupby("day").apply(lambda x: pd.Series({
        "mae_pooled": round(float(np.mean(np.abs(x.err_model))), 2),
        "mae_dow": round(float(np.mean(np.abs(x.err_dow.dropna()))), 2)
        if x.err_dow.notna().any() else None,
        "mae_city": round(float(np.mean(np.abs(x.err_city))), 2),
        "n_dow_cells": int(x.pred_dow.notna().sum()), "n": int(len(x)),
    }), include_groups=False).reset_index()
    print(per_day.to_string(index=False))


if __name__ == "__main__":
    main()
