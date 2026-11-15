"""
Stage 5 (real, pre-GTFS): aggregate per-day ping-pair speeds into
network-wide route x hour model tables, weighted by pair counts / distances.

Outputs:
  model/route_hour_real.parquet   (route_id, hour_ist, speed_kmh, n)
  model/route_speed_real.parquet  (route_id, speed_kmh, n)
  model/city_hour_real.parquet    (hour_ist, speed_kmh, n)
"""
import glob
import json
import datetime as dt

import pandas as pd

from common import CLEAN, MODEL


def main():
    sp = sorted(CLEAN.glob("speeds_*.parquet"))
    if not sp:
        raise SystemExit("[05r] no speeds_*.parquet — run 04f first")
    frames = [pd.read_parquet(p).assign(day=p.stem.replace("speeds_", ""))
              for p in sp]
    df = pd.concat(frames, ignore_index=True)

    # (route, hour): pair-count weighted mean across days
    rh = (df.groupby(["route_id", "hour_ist"])
            .apply(lambda x: pd.Series({
                "speed_kmh": (x.speed_kmh * x.n).sum() / max(1, x.n.sum()),
                "n": int(x.n.sum()), "days": int(len(x))}),
                   include_groups=False)
            .reset_index())

    # route overall: from per-day aggregate trip-weighted averages
    agg_frames = [pd.read_parquet(p) for p in sorted(CLEAN.glob("speedsagg_*.parquet"))]
    a = pd.concat(agg_frames, ignore_index=True)
    a["sp"] = a.dist_sum_m / a.dt_sum_s * 3.6
    rs = (a.groupby("route_id")
            .apply(lambda x: pd.Series({
                "speed_kmh": x.dist_sum_m.sum() / max(1e-9, x.dt_sum_s.sum()) * 3.6,
                "n": int(1.0 / max(1, len(x)) * 0 + len(x)), "days": len(x)}),
                   include_groups=False)
            .reset_index())

    # city hour: pair-weighted mean across routes
    ch = (df.groupby("hour_ist")
            .apply(lambda x: pd.Series({
                "speed_kmh": (x.speed_kmh * x.n).sum() / max(1, x.n.sum()),
                "n": int(x.n.sum())}), include_groups=False)
            .reset_index())

    rh.to_parquet(MODEL / "route_hour_real.parquet", index=False)
    rs.to_parquet(MODEL / "route_speed_real.parquet", index=False)
    ch.to_parquet(MODEL / "city_hour_real.parquet", index=False)

    meta = {
        "version": "v1-pings-real",
        "generated_utc": dt.datetime.utcnow().isoformat(),
        "source_days": sorted({p.stem.replace("speeds_", "") for p in sp}),
        "n_route_hour_cells": int(len(rh)),
        "n_routes": int(rh.route_id.nunique()),
        "n_city_hour_cells": int(len(ch)),
        "note": "network speeds from ping pairs; stop-level quantiles arrive with GTFS",
    }
    (MODEL / "meta_pings.json").write_text(json.dumps(meta, indent=2))
    print(f"[05r] routes: {rh.route_id.nunique()} | route x hour cells: {len(rh)} | "
          f"days: {len(meta['source_days'])}")
    pr = ch[(ch.hour_ist == 9) | (ch.hour_ist == 21)]
    if len(pr) == 2:
        print(f"[05r] city 09:00 {pr.iloc[0].speed_kmh:.1f} km/h | "
              f"21:00 {pr.iloc[1].speed_kmh:.1f} km/h (pair-weighted) ")


if __name__ == "__main__":
    main()
