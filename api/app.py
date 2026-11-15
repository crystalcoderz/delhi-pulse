"""
Delhi Pulse journey planner API (v0: direct routes, calibrated medians).

Endpoints:
  GET  /health
  GET  /stops/search?q=...
  GET  /journeys?from=...&to=...&depart=ISO8601
  GET  /pulse                       (city speed curve placeholder)

from/to formats: "lat,lng" | "stop_name_text" (fuzzy) | "stop:<stop_id>"
"""
import json
import math
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from rapidfuzz import process as fuzz_process

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import GTFS, MODEL, ROOT, WEB  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WALK_KMH = 4.8
WALK_ROUTE_FACTOR = 1.30
DEFAULT_WAIT_S = 300.0          # 5 min flat until headway model lands
MAX_STOPS_NEAR = 5
NEAR_KM = 2.5

app = FastAPI(title="Delhi Pulse", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# ---------------------------------------------------------------- static GTFS
stops = pd.read_csv(GTFS / "stops.txt")
routes = pd.read_csv(GTFS / "routes.txt").set_index("route_id")
trips = pd.read_csv(GTFS / "trips.txt")
stimes = pd.read_csv(GTFS / "stop_times.txt")

stop_pos = {r.stop_id: (r.stop_lat, r.stop_lon, r.stop_name)
            for r in stops.itertuples()}
routes_short = {r: (routes.loc[r].route_short_name if r in routes.index else str(r))
                for r in trips.route_id.unique()}

_slat = stops.stop_lat.to_numpy()
_slng = stops.stop_lon.to_numpy()
_RAD = np.radians


def nearest_stops(lat, lng, k=MAX_STOPS_NEAR, radius_km=NEAR_KM):
    d = np.sqrt(((lng - _slng) * 111320.0 * np.cos(_RAD(lat)) / 1000.0) ** 2 +
                ((lat - _slat) * 110540.0 / 1000.0) ** 2)   # km
    idx = np.argsort(d)[:k]
    out = []
    for i in idx:
        if d[i] <= radius_km:
            sid = stops.stop_id.iloc[i]
            out.append(dict(stop_id=sid, name=stops.stop_name.iloc[i],
                            lat=float(_slat[i]), lng=float(_slng[i]),
                            walk_min=round(d[i] * 1000 * WALK_ROUTE_FACTOR / (WALK_KMH * 1000 / 60), 1)))
    return out


# ---- build route patterns (distinct stop sequences per route+direction) ----
PAT = []       # list of dict(route_id, stops=list[stop_id], leg_m=list)
STOP_MEMBER = {}  # stop_id -> list[(pat_idx, pos)]

_g = stimes.sort_values(["trip_id", "stop_sequence"]).groupby("trip_id")
for tid, g in _g:
    seq = list(g.stop_id)
    key = str(trips.set_index("trip_id").route_id.get(tid))
    PAT.append(dict(route_id=key, stops=seq, tid=tid))
for pi, p in enumerate(PAT):
    for pos, sid in enumerate(p["stops"]):
        STOP_MEMBER.setdefault(sid, []).append((pi, pos))
# dedupe identical sequences per route to shrink the index
# (cheap approach: keep as-is; v0.5 will collapse duplicates)
for p in PAT:
    legs = []
    for a, b in zip(p["stops"][:-1], p["stops"][1:]):
        la, lo, _ = stop_pos[a]
        lb, lo2, _ = stop_pos[b]
        legs.append(math.hypot((lo2 - lo) * 111320.0 * math.cos(_RAD(la)),
                              (lb - la) * 110540.0))
    p["leg_m"] = legs

# ---- model lookups ----
def _load(name):
    p = MODEL / name
    if not p.exists():
        return pd.DataFrame()
    return pd.read_parquet(p)

edge_hour = _load("edge_hour.parquet")
route_hour = _load("route_hour.parquet")
route_speed = _load("route_speed.parquet")
city_hour = _load("city_hour.parquet")

E50 = {(str(r.route_id), str(r.from_stop), str(r.to_stop), int(r.hour_ist)): float(r.p50_s)
       for r in edge_hour.itertuples()} if len(edge_hour) else {}
RH = {(str(r.route_id), int(r.hour_ist)): float(r.speed_kmh)
      for r in route_hour.itertuples()} if len(route_hour) else {}
RS = {str(r.route_id): float(r.speed_kmh) for r in route_speed.itertuples()} if len(route_speed) else {}
CH = {int(r.hour_ist): r.speed_kmh for r in city_hour.itertuples()} if len(city_hour) else {}
CH_DEFAULT = float(np.median(list(CH.values()))) if CH else 15.0


def leg_seconds(route_id, a, b, dist_m, hour):
    e = E50.get((route_id, a, b, hour))
    if e:
        return float(e)
    sp = RH.get((route_id, hour)) or RS.get(route_id) or CH.get(hour) or CH_DEFAULT
    return dist_m * 3.6 / max(1.0, sp)   # meters at km/h -> seconds


def resolve_place(q: str):
    q = (q or "").strip()
    try:
        lat, lng = [float(x) for x in q.split(",")]
        return dict(lat=lat, lng=lng, label=f"{lat:.5f}, {lng:.5f}")
    except ValueError:
        pass
    if q.startswith("stop:"):
        sid = q[5:]
        if sid in stop_pos:
            la, lo, nm = stop_pos[sid]
            return dict(lat=la, lng=lo, label=nm, stop_id=sid)
        raise HTTPException(404, f"unknown stop {sid}")
    names = stops.stop_name.tolist()
    hit = fuzz_process.extractOne(q, names, score_cutoff=55)
    if not hit:
        raise HTTPException(404, f"cannot find {q!r}")
    sid = stops.stop_id.iloc[hit[2]]
    la, lo, nm = stop_pos[sid]
    return dict(lat=la, lng=lo, label=nm, stop_id=sid)


@app.get("/health")
def health():
    return dict(ok=True, patterns=len(PAT), edges=len(E50), routes=len(RS))


@app.get("/stops/search")
def stops_search(q: str = Query(..., min_length=1), limit: int = 12):
    names = stops.stop_name.tolist()
    hits = fuzz_process.extract(q, names, limit=limit)
    out = []
    for name, score, i in hits:
        sid = stops.stop_id.iloc[i]
        la, lo, _ = stop_pos[sid]
        out.append(dict(stop_id=sid, name=name, lat=la, lng=lo, score=int(score)))
    return out


def _bus_leg_coords(pat, i, j):
    return [[stop_pos[s][0], stop_pos[s][1]] for s in pat["stops"][i:j + 1]]


@app.get("/journeys")
def journeys(fromq: str = Query(..., alias="from"),
             toq: str = Query(..., alias="to"),
             depart: str | None = None):
    o = resolve_place(fromq)
    d = resolve_place(toq)
    dep = dt.datetime.fromisoformat(depart) if depart else dt.datetime.now(IST)
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=IST)
    dep = dep.astimezone(IST)
    hour0 = dep.hour

    o_near = nearest_stops(o["lat"], o["lng"]) or \
        nearest_stops(o["lat"], o["lng"], k=5, radius_km=6)
    d_near = nearest_stops(d["lat"], d["lng"]) or \
        nearest_stops(d["lat"], d["lng"], k=5, radius_km=6)
    if not o_near or not d_near:
        raise HTTPException(404, "no plausible stop access found")

    options = []
    for oa in o_near:
        for db_ in d_near:
            for pi, i in STOP_MEMBER.get(oa["stop_id"], []):
                pat = PAT[pi]
                for pj, j in STOP_MEMBER.get(db_["stop_id"], []):
                    if pi != pj or j <= i:
                        continue
                    board_epoch = dep.timestamp() + oa["walk_min"] * 60 + DEFAULT_WAIT_S
                    t_cum = board_epoch
                    ride = 0.0
                    for k in range(i, j):
                        seg_h = int((t_cum + 19800) // 3600 % 24)  # IST hour at leg start
                        s_leg = leg_seconds(pat["route_id"], pat["stops"][k],
                                            pat["stops"][k + 1], pat["leg_m"][k], seg_h)
                        ride += s_leg
                        t_cum += s_leg
                    ride_f = ride
                    total = oa["walk_min"] * 60 + DEFAULT_WAIT_S + ride_f + db_["walk_min"] * 60
                    options.append(dict(
                        _key=(pat["route_id"], oa["stop_id"]),
                        total_min=round(total / 60, 1),
                        walk_o=oa["walk_min"], walk_d=db_["walk_min"],
                        wait_min=DEFAULT_WAIT_S / 60,
                        ride_min=round(ride_f / 60, 1),
                        route_id=pat["route_id"],
                        route_name=routes_short.get(pat["route_id"], str(pat["route_id"])),
                        board=oa["stop_id"], board_name=oa["name"],
                        alight=db_["stop_id"], alight_name=db_["name"],
                        board_at=(dep + dt.timedelta(
                            minutes=oa["walk_min"] + DEFAULT_WAIT_S / 60)).isoformat(),
                        arrive=(dep + dt.timedelta(
                            minutes=total / 60)).isoformat(),
                        legs=[
                            dict(type="walk", minutes=oa["walk_min"],
                                 coords=[[o["lat"], o["lng"]],
                                         [oa["lat"], oa["lng"]]]),
                            dict(type="bus", minutes=round(ride_f / 60, 1),
                                 route=routes_short.get(pat["route_id"], str(pat["route_id"])),
                                 stops=_bus_leg_coords(pat, i, j)),
                            dict(type="walk", minutes=db_["walk_min"],
                                 coords=[[db_["lat"], db_["lng"]],
                                         [d["lat"], d["lng"]]]),
                        ],
                        eta_p50_min=round(total / 60, 1),
                        eta_p10_min=round((oa["walk_min"] + DEFAULT_WAIT_S / 60
                                           + ride_f * 0.85) / 60, 1),
                        eta_p90_min=round((oa["walk_min"] + DEFAULT_WAIT_S / 60
                                           + ride_f * 1.35) / 60, 1),
                        usual_p50_min=round(total / 60, 1),
                        confidence="v0-medians",
                    ))

    if not options:
        return dict(options=[], note="no direct route found (transfers arrive in v0.5)")

    options.sort(key=lambda x: x["total_min"])
    seen, picked = set(), []
    for op in options:
        if op["_key"] in seen:
            continue
        seen.add(op["_key"])
        op.pop("_key")
        picked.append(op)
        if len(picked) == 3:
            break
    return dict(options=picked)


@app.get("/pulse")
def pulse():
    return dict(city_hour={str(k): round(v, 2) for k, v in sorted(CH.items())},
                note="hour -> observed mean network speed km/h (full overlay in v1)")


if (ROOT / "web").exists():
    app.mount("/web", StaticFiles(directory=str(WEB), html=True), name="web")
