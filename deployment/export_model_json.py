"""Export the pattern index as a compressed JSON asset for the Worker.

The edge planner needs:
- patterns (route + ordered stop sequence)
- stop positions + names
- membership index (stop → which patterns serve it and at what position)

Output: workers/public/model.json  (fetched by the Worker at startup)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import pandas as pd
from common import GTFS, MODEL

# ---- recreate the pattern index (same logic as api/app.py) ----
trips = pd.read_csv(GTFS / "trips.txt", dtype=str)
stops_df = pd.read_csv(GTFS / "stops.txt")
stops_df["stop_id"] = stops_df["stop_id"].astype(str)
stop_pos = {r.stop_id: (r.stop_lat, r.stop_lon, r.stop_name)
             for r in stops_df.itertuples()}

# ---- load cached patterns if available ----
import hashlib, pickle
_st = (GTFS / "stop_times.txt").stat().st_mtime
_key = hashlib.md5(str(_st).encode()).hexdigest()[:10]
_cp = MODEL / f"patterns_{_key}.pkl"
if _cp.exists():
    PAT, STOP_MEMBER = pickle.load(open(_cp, "rb"))
else:
    stimes = pd.read_csv(GTFS / "stop_times.txt", dtype=str)
    stimes["stop_sequence"] = stimes.stop_sequence.astype(int)
    _g = stimes.sort_values(["trip_id", "stop_sequence"]).groupby("trip_id")
    PAT, STOP_MEMBER = [], {}
    _seen = {}
    tmap = trips.set_index("trip_id").route_id
    for tid, g in _g:
        seq = list(g.stop_id)
        key = str(tmap.get(tid))
        pk = (key, tuple(seq))
        if pk in _seen: continue
        _seen[pk] = len(PAT)
        PAT.append(dict(route_id=key, stops=seq))
    MEMBER_CAP = 80
    for pi, p in enumerate(PAT):
        for pos, sid in enumerate(p["stops"]):
            lst = STOP_MEMBER.setdefault(sid, [])
            if len(lst) < MEMBER_CAP: lst.append((pi, pos))
    pickle.dump((PAT, STOP_MEMBER),
                open(_cp, "wb"), protocol=pickle.HIGHEST_PROTOCOL)

# ---- export as compact JSON (uncapped membership!) ----
# Build fresh: patterns_out AND UNCAPPED membership so edge search finds all routes
patterns_out = []
for p in PAT:
    patterns_out.append({"r": p["route_id"], "s": p["stops"]})

member_out = {}  # <-- UNCAPPED for edge
for pi, p in enumerate(PAT):
    for pos, sid in enumerate(p["stops"]):
        member_out.setdefault(sid, []).append([pi, pos])

stops_out = {}
for sid, (lat, lng, name) in stop_pos.items():
    stops_out[sid] = [round(lat, 6), round(lng, 6), name]

# ---- route names ----
routes_df = pd.read_csv(GTFS / "routes.txt", dtype=str)
routes_df["route_id"] = routes_df.route_id.astype(str)
_rn = dict(zip(routes_df.route_id,
               routes_df.route_short_name.fillna(routes_df.route_long_name)))

# ---- model tables for edge speed lookup ----
def _pqt(n):
    p = MODEL / n
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()

rh = _pqt("route_hour_real.parquet")
rs = _pqt("route_speed_real.parquet")
ch = _pqt("city_hour_real.parquet")

speeds = {}
for r in rh.itertuples():
    speeds.setdefault(str(r.route_id), {})[int(r.hour_ist)] = round(float(r.speed_kmh), 2)
route_avg = {str(r.route_id): round(float(r.speed_kmh), 2) for r in rs.itertuples()}
city_hr = {int(r.hour_ist): round(float(r.speed_kmh), 2) for r in ch.itertuples()}

headway = {}
wh_path = MODEL / "wait_headway_real.parquet"
if wh_path.exists():
    wh = _pqt("wait_headway_real.parquet")
    for r in wh.itertuples():
        headway.setdefault(str(r.route_id), {})[int(r.hour_ist)] = round(
            float(r.headway_s), 1)

model = {
    "patterns": patterns_out,
    "membership": member_out,
    "stops": stops_out,
    "route_names": _rn,
    "speeds": speeds,
    "route_avg": route_avg,
    "city_hour": city_hr,
    "headway": headway,
}

OUT = Path(__file__).resolve().parent.parent / "workers" / "public" / "model.json"
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(model, separators=(",", ":")), encoding="utf-8")
SIZE_MB = OUT.stat().st_size / 1e6

# Also create a gzip'd copy for efficient fetch
import gzip
with gzip.open(str(OUT) + ".gz", "wt", encoding="utf-8") as f:
    json.dump(model, f, separators=(",", ":"))
GZ_MB = Path(str(OUT) + ".gz").stat().st_size / 1e6

print(f"model.json: {SIZE_MB:.1f} MB")
print(f"model.json.gz: {GZ_MB:.1f} MB")
print(f"patterns: {len(patterns_out)}, stops: {len(stops_out)},")
print(f"  membership: {len(member_out)}, speeds: {len(speeds)},")
print(f"  headways: {len(headway)} routes x hours")
