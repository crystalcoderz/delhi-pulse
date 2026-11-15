"""Bisect the API startup wedge: phase-by-phase re-import with progress prints."""
import sys
import time
from pathlib import Path

T0 = time.time()


def mark(label):
    print(f"[{time.time()-T0:8.1f}s] {label}", flush=True)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
mark("path set")

import pandas as pd  # noqa: E402
mark("pandas")

from common import CLEAN, GTFS, MODEL, ROOT, WEB  # noqa: E402
mark("common")

stops = pd.read_csv(GTFS / "stops.txt")
stops["stop_id"] = stops["stop_id"].astype(str)
mark("stops")
routes = pd.read_csv(GTFS / "routes.txt")
routes["route_id"] = routes["route_id"].astype(str)
routes = routes.set_index("route_id")
mark("routes")
trips = pd.read_csv(GTFS / "trips.txt", dtype=str)
mark("trips")

import hashlib  # noqa: E402
import pickle  # noqa: E402
_st = (GTFS / "stop_times.txt").stat().st_mtime
_key = hashlib.md5(str(_st).encode()).hexdigest()[:10]
_cp = MODEL / f"patterns_{_key}.pkl"
mark(f"cache key {_key}; exists: {_cp.exists()}")
PAT, STOP_MEMBER = [], {}
if _cp.exists():
    with open(_cp, "rb") as f:
        PAT, STOP_MEMBER = pickle.load(f)
    mark(f"pickle loaded: {len(PAT)} patterns, {len(STOP_MEMBER)} stops")
else:
    stimes = pd.read_csv(GTFS / "stop_times.txt", dtype=str)
    mark(f"stimes cold read: {len(stimes):,} rows")
    stimes["stop_sequence"] = stimes.stop_sequence.astype(int)
    mark("stop_sequence cast")
    _g = stimes.sort_values(["trip_id", "stop_sequence"]).groupby("trip_id")
    mark("groupby built")
    tmap = trips.set_index("trip_id").route_id
    mark("trip->route map")
    _seen = {}
    n = 0
    for tid, g in _g:
        n += 1
        if n % 20000 == 0:
            mark(f"groups {n:,}")
        seq = list(g.stop_id)
        key = str(tmap.get(tid))
        pk = (key, tuple(seq))
        if pk in _seen:
            continue
        _seen[pk] = len(PAT)
        PAT.append(dict(route_id=key, stops=seq, tid=tid))
    mark(f"patterns built: {len(PAT)}")
    MEMBER_CAP = 80
    for pi, p in enumerate(PAT):
        for pos, sid in enumerate(p["stops"]):
            lst = STOP_MEMBER.setdefault(sid, [])
            if len(lst) < MEMBER_CAP:
                lst.append((pi, pos))
    mark("membership built")
    with open(_cp, "wb") as f:
        pickle.dump((PAT, STOP_MEMBER), f, protocol=pickle.HIGHEST_PROTOCOL)
    mark(f"pickle dumped {(_cp.stat().st_size/1e6):.1f} MB")

routes_short = {}
mark("routes_short start")
for r in trips.route_id.unique():
    routes_short[r] = (routes.loc[r].route_short_name if r in routes.index else str(r))
mark("routes_short done")

stop_pos = {r.stop_id: (r.stop_lat, r.stop_lon, r.stop_name)
            for r in stops.itertuples()}
mark(f"stop_pos {len(stop_pos)}")

mark("leg_m loop start")
_slat = stops.stop_lat.to_numpy()
mark("lat array")
total_legs = 0
for p in PAT:
    for a, b in zip(p["stops"][:-1], p["stops"][1:]):
        la, lo, _ = stop_pos[a]
        lb, lo2, _ = stop_pos[b]
        total_legs += 1
mark(f"leg_m loop done ({total_legs} legs)")

import numpy as np  # noqa: E402
mark("numpy")


def _load(name):
    p = MODEL / name
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


for name in ("edge_hour.parquet", "route_hour.parquet", "route_speed.parquet",
             "city_hour.parquet", "headway.parquet", "headway_hour.parquet",
             "route_hour_real.parquet", "route_speed_real.parquet",
             "city_hour_real.parquet", "route_hour_dow_real.parquet",
             "wait_headway_real.parquet"):
    df = _load(name)
    mark(f"model {name}: {len(df)} rows")

mark("shapes read start")
_shp = pd.read_csv(GTFS / "shapes.txt")
mark(f"shapes rows {_shp.shape[0]}")
_SHAPES = {}
for sid, g in _shp.groupby("shape_id", sort=False):
    g = g.sort_values("shape_pt_sequence")
    _SHAPES[str(sid)] = list(zip(g.shape_pt_lat.astype(float),
                                  g.shape_pt_lon.astype(float)))
mark(f"shapes grouped {len(_SHAPES)}")

_ROUTE_SHAPES = {}
for r in trips.itertuples():
    _ROUTE_SHAPES.setdefault(str(r.route_id), str(r.shape_id))
mark(f"route shapes {len(_ROUTE_SHAPES)}")

mark("ALL PHASES COMPLETE — no wedge in this sequence")
