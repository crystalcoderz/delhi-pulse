"""Temporal accuracy of the route x hour ping model (leave-one-day-out).

Arms compared, every one honest (predicted from days the model never saw):
  - dow model:  (route, dow, hour)  learned from other same-weekday days
  - pooled:     (route, hour)       learned from all other days
  - city:       (hour)             naive city-hour baseline

Two comparisons are reported for day-of-week keying (fix E):
  * lift_dow_vs_pooled         - dow MAE vs pooled MAE on the INTERSECTION
                                 subset (cells where BOTH arms have a
                                 prediction).  Like-for-like: same cells.
  * lift_dow_vs_pooled_allrows - pooled MAE over every evaluated cell vs dow
                                 MAE over the cells the dow arm covers (the
                                 historical, asymmetric definition).  Kept so
                                 older comparisons remain auditable.

Pair-count weighting (fix E): every MAE is also reported with cells weighted by
the test day's observed ping-pair count (n), so a 500-pair cell does not
count the same as a 12-pair cell.  `n_pairs` is the count in the *test* cell;
the dow arm's own training support is not used as a weight.

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
DOW_SUBSET = "intersection"   # cells where both pooled and dow have a pred


def amean(vals):
    """Mean of the finite values, or None when nothing is finite."""
    a = np.asarray(vals, dtype=float)
    a = a[np.isfinite(a)]
    return float(a.mean()) if a.size else None


def wmean(vals, weights):
    """Pair-count-weighted mean of finite (value, weight) pairs, or None."""
    a = np.asarray(vals, dtype=float)
    w = np.asarray(weights, dtype=float)
    ok = np.isfinite(a) & np.isfinite(w) & (w > 0)
    if not ok.any():
        return None
    return float(np.average(a[ok], weights=w[ok]))


def rnd(x, nd=2):
    return None if x is None else round(float(x), nd)


def lift(better_mae, base_mae):
    """Percent improvement of better_mae over base_mae, None if undefined."""
    if better_mae is None or base_mae is None or base_mae == 0:
        return None
    return round((1 - better_mae / base_mae) * 100, 1)


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
        # n_pairs = observed pairs in the TEST day's (route, hour) cell;
        # renaming it up-front avoids pandas _x/_y suffixes on merge.
        test = df[df.day == d].rename(columns={"n": "n_pairs"})
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
        m = m[m.n_pairs >= MIN_PAIRS_CELL]
        m["err_model"] = m.pred - m.speed_kmh
        m["err_dow"] = m.pred_dow - m.speed_kmh
        m["err_city"] = m.city_pred - m.speed_kmh
        rows.append(m[["route_id", "dow_ist", "hour_ist", "day", "speed_kmh",
                       "n_pairs", "pred", "pred_dow", "city_pred",
                       "err_model", "err_dow", "err_city"]])
    r = pd.concat(rows, ignore_index=True)
    if r.empty:
        raise SystemExit(f"[H] no cells with >= {MIN_PAIRS_CELL} test-day pairs")

    abs_model = r.err_model.abs()
    abs_city = r.err_city.abs()
    abs_dow = r.err_dow.abs()
    w = r.n_pairs

    # ---- all-rows arm scores (unchanged historical definition) ----
    mae_m = amean(abs_model)
    mae_c = amean(abs_city)
    mae_d = amean(abs_dow)          # dow only where it has a prediction

    # ---- intersection subset: cells both arms can score ----
    both = r[r.pred_dow.notna()]
    mae_m_int = amean(both.err_model.abs())
    mae_d_int = amean(both.err_dow.abs())

    # ---- pair-count-weighted versions ----
    mae_m_w = wmean(abs_model, w)
    mae_c_w = wmean(abs_city, w)
    mae_d_w = wmean(abs_dow, w)
    mae_m_int_w = wmean(both.err_model.abs(), both.n_pairs)
    mae_d_int_w = wmean(both.err_dow.abs(), both.n_pairs)

    mape_m = amean(abs_model / r.speed_kmh.clip(lower=1) * 100)
    within10 = amean((abs_model <= 10).astype(float))
    within5 = amean((abs_model <= 5).astype(float))

    rep = dict(
        test="leave-one-day-out route x hour speeds",
        days=days, n_cells_evald=int(len(r)),
        n_cells_dow_evald=int(r.pred_dow.notna().sum()),
        n_cells_intersection=int(len(both)),
        dow_subset=DOW_SUBSET,
        mae_model_kmh=rnd(mae_m),
        mae_model_kmh_weighted=rnd(mae_m_w),
        mae_dow_model_kmh=rnd(mae_d),
        mae_dow_model_kmh_weighted=rnd(mae_d_w),
        mae_city_baseline_kmh=rnd(mae_c),
        mae_city_baseline_kmh_weighted=rnd(mae_c_w),
        mae_pooled_intersection_kmh=rnd(mae_m_int),
        mae_dow_intersection_kmh=rnd(mae_d_int),
        lift_vs_baseline=lift(mae_m, mae_c),
        lift_dow_vs_pooled=lift(mae_d_int, mae_m_int),
        lift_dow_vs_pooled_weighted=lift(mae_d_int_w, mae_m_int_w),
        lift_dow_vs_pooled_allrows=lift(mae_d, mae_m),
        mape_model_pct=rnd(mape_m, 1),
        within_5kmh=rnd(within5, 3),
        within_10kmh=rnd(within10, 3),
    )
    (MODEL / "holdout_pings.json").write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))

    per_day = r.groupby("day").apply(lambda x: pd.Series({
        "mae_pooled": rnd(amean(x.err_model.abs())),
        "mae_pooled_w": rnd(wmean(x.err_model.abs(), x.n_pairs)),
        "mae_dow": rnd(amean(x.err_dow.abs())),
        "mae_dow_w": rnd(wmean(x.err_dow.abs(), x.n_pairs)),
        "mae_city": rnd(amean(x.err_city.abs())),
        "n_dow_cells": int(x.pred_dow.notna().sum()), "n": int(len(x)),
    }), include_groups=False).reset_index()
    print(per_day.to_string(index=False))
    if rep["n_cells_intersection"] == 0:
        print("[H] WARNING: no cell is scored by both arms; "
              "lift_dow_vs_pooled is null")
    elif rep["lift_dow_vs_pooled"] is None:
        print("[H] note: lift_dow_vs_pooled undefined "
              "(zero pooled MAE on the intersection subset)")


if __name__ == "__main__":
    main()
