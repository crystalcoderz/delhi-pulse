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
_seen = {}   # (route_id, stops-tuple) -> pattern index, collapses ~89k trips to distinct patterns
for tid, g in _g:
    seq = list(g.stop_id)
    key = str(trips.set_index("trip_id").route_id.get(tid))
    pk = (key, tuple(seq))
    if pk in _seen:
        continue
    _seen[pk] = len(PAT)
    PAT.append(dict(route_id=key, stops=seq, tid=tid))
for pi, p in enumerate(PAT):
    for pos, sid in enumerate(p["stops"]):
        STOP_MEMBER.setdefault(sid, []).append((pi, pos))
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
headway = _load("headway.parquet")

E50 = {(str(r.route_id), str(r.from_stop), str(r.to_stop), int(r.hour_ist)): float(r.p50_s)
       for r in edge_hour.itertuples()} if len(edge_hour) else {}
RH = {(str(r.route_id), int(r.hour_ist)): float(r.speed_kmh)
      for r in route_hour.itertuples()} if len(route_hour) else {}
RS = {str(r.route_id): float(r.speed_kmh) for r in route_speed.itertuples()} if len(route_speed) else {}
CH = {int(r.hour_ist): r.speed_kmh for r in city_hour.itertuples()} if len(city_hour) else {}
CH_DEFAULT = float(np.median(list(CH.values()))) if CH else 15.0
WAIT50 = {(str(r.route_id), str(r.stop_id)): float(r.h50_s)
          for r in headway.itertuples()} if len(headway) else {}


def wait_minutes(route_id, stop_id):
    """Expected wait from observed headways (p50/2), clamped 2..15 min."""
    h = WAIT50.get((str(route_id), str(stop_id)))
    if h:
        return min(15.0, max(2.0, h / 120.0))
    return DEFAULT_WAIT_S / 60.0


def ride_time(pat, i, j, start_epoch):
    """Sum leg seconds from pos i to j with hour refinement along the way."""
    t = start_epoch
    total = 0.0
    for k in range(i, j):
        h = int((t + 19800) // 3600 % 24)  # IST hour at leg start
        s = leg_seconds(pat["route_id"], pat["stops"][k], pat["stops"][k + 1],
                        pat["leg_m"][k], h)
        total += s
        t += s
    return total, t


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

    o_near = nearest_stops(o["lat"], o["lng"]) or \
        nearest_stops(o["lat"], o["lng"], k=5, radius_km=6)
    d_near = nearest_stops(d["lat"], d["lng"]) or \
        nearest_stops(d["lat"], d["lng"], k=5, radius_km=6)
    if not o_near or not d_near:
        raise HTTPException(404, "no plausible stop access found")

    def route_name(rid):
        return routes_short.get(str(rid), str(rid))

    TRANSFER_BUFFER_S = 120.0
    options = []
    dep_epoch = dep.timestamp()

    # ---------------- direct options ----------------
    for oa in o_near:
        for db_ in d_near:
            for pi, i in STOP_MEMBER.get(oa["stop_id"], []):
                pat = PAT[pi]
                for pj, j in STOP_MEMBER.get(db_["stop_id"], []):
                    if pi != pj or j <= i:
                        continue
                    wait_a = wait_minutes(pat["route_id"], oa["stop_id"])
                    board_epoch = dep_epoch + oa["walk_min"] * 60 + wait_a * 60
                    ride_f, _ = ride_time(pat, i, j, board_epoch)
                    total = (oa["walk_min"] + wait_a + ride_f / 60 + db_["walk_min"])
                    options.append(dict(
                        _key=(pat["route_id"], oa["stop_id"]),
                        transfers=0,
                        total_min=round(total, 1),
                        walk_o=oa["walk_min"], walk_d=db_["walk_min"],
                        wait_min=round(wait_a, 1),
                        ride_min=round(ride_f / 60, 1),
                        route_id=str(pat["route_id"]),
                        route_name=route_name(pat["route_id"]),
                        board=oa["stop_id"], board_name=oa["name"],
                        alight=db_["stop_id"], alight_name=db_["name"],
                        board_at=(dep + dt.timedelta(
                            minutes=oa["walk_min"] + wait_a)).isoformat(),
                        arrive=(dep + dt.timedelta(minutes=total)).isoformat(),
                        legs=[
                            dict(type="walk", minutes=oa["walk_min"],
                                 coords=[[o["lat"], o["lng"]],
                                         [oa["lat"], oa["lng"]]]),
                            dict(type="bus", minutes=round(ride_f / 60, 1),
                                 route=route_name(pat["route_id"]),
                                 stops=_bus_leg_coords(pat, i, j)),
                            dict(type="walk", minutes=db_["walk_min"],
                                 coords=[[db_["lat"], db_["lng"]],
                                         [d["lat"], d["lng"]]]),
                        ],
                        eta_p50_min=round(total, 1),
                        eta_p10_min=round(oa["walk_min"] + wait_a
                                          + ride_f * 0.85 / 60, 1),
                        eta_p90_min=round(oa["walk_min"] + wait_a
                                          + ride_f * 1.35 / 60, 1),
                        usual_p50_min=round(total, 1),
                        confidence="v0.5-transfers",
                    ))

    # ---------------- one-transfer options ----------------
    for oa in o_near:
        for db_ in d_near:
            for pi, i in STOP_MEMBER.get(oa["stop_id"], []):
                A = PAT[pi]
                apos = {s: k for k, s in enumerate(A["stops"])}
                for pj, j in STOP_MEMBER.get(db_["stop_id"], []):
                    B = PAT[pj]
                    if pi == pj or A["route_id"] == B["route_id"]:
                        continue
                    for tb, tstop in enumerate(B["stops"]):
                        ia = apos.get(tstop)
                        if ia is None or ia <= i or tb >= j:
                            continue
                        wait_a = wait_minutes(A["route_id"], oa["stop_id"])
                        board_epoch = dep_epoch + oa["walk_min"] * 60 + wait_a * 60
                        rideA, t_at_tr = ride_time(A, i, ia, board_epoch)
                        wait_b = wait_minutes(B["route_id"], tstop)
                        t_board2 = t_at_tr + TRANSFER_BUFFER_S + wait_b * 60
                        rideB, _ = ride_time(B, tb, j, t_board2)
                        total = (oa["walk_min"] + wait_a + rideA / 60
                                 + (TRANSFER_BUFFER_S + wait_b * 60) / 60
                                 + rideB / 60 + db_["walk_min"])
                        options.append(dict(
                            _key=(A["route_id"], B["route_id"], tstop),
                            transfers=1,
                            total_min=round(total, 1),
                            walk_o=oa["walk_min"], walk_d=db_["walk_min"],
                            wait_min=round(wait_a + TRANSFER_BUFFER_S / 60
                                           + wait_b, 1),
                            ride_min=round((rideA + rideB) / 60, 1),
                            route_id=f"{A['route_id']}+{B['route_id']}",
                            route_name=f"{route_name(A['route_id'])} + "
                                       f"{route_name(B['route_id'])}",
                            board=oa["stop_id"], board_name=oa["name"],
                            alight=db_["stop_id"], alight_name=db_["name"],
                            transfer_stop=tstop,
                            transfer_name=stop_pos[tstop][2] if tstop in stop_pos else tstop,
                            board_at=(dep + dt.timedelta(
                                minutes=oa["walk_min"] + wait_a)).isoformat(),
                            arrive=(dep + dt.timedelta(minutes=total)).isoformat(),
                            legs=[
                                dict(type="walk", minutes=oa["walk_min"],
                                     coords=[[o["lat"], o["lng"]],
                                             [oa["lat"], oa["lng"]]]),
                                dict(type="bus", minutes=round(rideA / 60, 1),
                                     route=route_name(A["route_id"]),
                                     stops=_bus_leg_coords(A, i, ia)),
                                dict(type="transfer",
                                     minutes=round(TRANSFER_BUFFER_S / 60
                                                   + wait_b, 1),
                                     at=tstop),
                                dict(type="bus", minutes=round(rideB / 60, 1),
                                     route=route_name(B["route_id"]),
                                     stops=_bus_leg_coords(B, tb, j)),
                                dict(type="walk", minutes=db_["walk_min"],
                                     coords=[[db_["lat"], db_["lng"]],
                                             [d["lat"], d["lng"]]]),
                            ],
                            eta_p50_min=round(total, 1),
                            eta_p10_min=round(oa["walk_min"]
                                              + wait_a + TRANSFER_BUFFER_S / 60 + wait_b
                                              + (rideA + rideB) * 0.85 / 60, 1),
                            eta_p90_min=round(oa["walk_min"]
                                              + wait_a + TRANSFER_BUFFER_S / 60 + wait_b
                                              + (rideA + rideB) * 1.35 / 60, 1),
                            usual_p50_min=round(total, 1),
                            confidence="v0.5-transfers",
                        ))
    if not options:
        return dict(options=[], note="no route found via max 1 transfer in range")

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
