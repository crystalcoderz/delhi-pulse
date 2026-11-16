"""Export the pattern index as a compressed JSON asset for the Worker.

The edge planner needs:
- patterns (route + ordered stop sequence)
- stop positions + names
- membership index (stop → which patterns serve it and at what position)

Fix-plan §B additions (all additive to the frozen API contract):
- `model_source` "traversal"|"ping-pair" — never a silent *_real fallback;
- `speeds_n` / `route_avg_n` sample counts (C6, int-truncated);
- `usual_hour` = median-speed off-peak hour (C7, no widening past 11-17);
- `wait_gap` observed-gap waits [mean_gap_s, gap_cv] next to `headway` (B5);
- `band_cal` [lo_factor, hi_factor] calibrated on the journey holdout (B7);
- `detour` gated to routes with >= 2 effective (chord_m>0) edges.

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

# B1: serve TRAVESAL speeds (stage 04: SUM(dist_m)/SUM(dur_s) over edge
# traversals — dwell-inclusive, along-route), not the ping-pair tables that
# exclude time stopped at stops. Ping tables stay as a defensive fallback.
# §B missing-file guard: the fallback must never be silent — warn loudly and
# stamp which table family actually fed the export (`model_source`) so
# downstream tests can assert the speed source.
def _load_pair(preferred, fallback):
    p, f = MODEL / preferred, MODEL / fallback
    if p.exists():
        df = pd.read_parquet(p)
        if len(df):
            return df, True
        print(f"WARNING: {preferred} present but empty — treating as missing")
    if f.exists():
        print(f"WARNING: {preferred} missing — using ping-pair fallback "
              f"{fallback}")
        return pd.read_parquet(f), False
    print(f"WARNING: neither {preferred} nor {fallback} present — "
          f"exporting without this table")
    return pd.DataFrame(), False

rh, rh_trav = _load_pair("route_hour.parquet", "route_hour_real.parquet")
rs, rs_trav = _load_pair("route_speed.parquet", "route_speed_real.parquet")
ch, ch_trav = _load_pair("city_hour.parquet", "city_hour_real.parquet")
# the (route, hour) speed table is the primary served source; the tag follows it
model_source = "traversal" if rh_trav else "ping-pair"
if not rs_trav or not ch_trav:
    print(f"WARNING: speed source mix — route_hour={'traversal' if rh_trav else 'ping-pair'}, "
          f"route_speed={'traversal' if rs_trav else 'ping-pair'}, "
          f"city_hour={'traversal' if ch_trav else 'ping-pair'}")

speeds = {}
speeds_n = {}    # C6: route -> hour -> sample count behind the speed (int)
for r in rh.itertuples():
    _rid = str(r.route_id)
    _h = int(r.hour_ist)
    speeds.setdefault(_rid, {})[_h] = round(float(r.speed_kmh), 2)
    _n = getattr(r, "n", None)
    if _n is not None and pd.notna(_n):
        speeds_n.setdefault(_rid, {})[_h] = int(_n)
route_avg = {}
route_avg_n = {}  # C6: route -> sample count behind route_avg (int)
for r in rs.itertuples():
    route_avg[str(r.route_id)] = round(float(r.speed_kmh), 2)
    _n = getattr(r, "n", None)
    if _n is not None and pd.notna(_n):
        route_avg_n[str(r.route_id)] = int(_n)
city_hr = {int(r.hour_ist): round(float(r.speed_kmh), 2) for r in ch.itertuples()}

# ---- B9: per-route detour factor = along-route distance / chord distance.
# The planner measures chords between consecutive stops; observed traversals
# measure the along-route length. The factor trues the chord up.
# §B top: gate the export with >= 2 effective edges per route — a factor from
# a single chord_m>0 traversal is noise, so such routes omit the key.
detour = {}
try:
    import duckdb as _dd
    _con = _dd.connect()
    _con.execute("SET memory_limit='6GB'")
    _con.register("sf", stops_df.rename(columns={
        "stop_id": "stop_id", "stop_lat": "lat", "stop_lon": "lng"})[["stop_id", "lat", "lng"]])
    _con.register("st", stops_df[["stop_id", "stop_lat", "stop_lon"]]
                  .rename(columns={"stop_lat": "lat", "stop_lon": "lng"}))
    _tfiles = ", ".join(f"'{str(p).replace(chr(92), '/')}'"
                        for p in sorted((Path(__file__).resolve().parent.parent
                                         / "data" / "clean").glob("traversals_*.parquet")))
    if _tfiles:
        df = _con.execute(f"""
            WITH per AS (
                SELECT t.route_id, t.dist_m,
                       2 * 6371000.0 * asin(sqrt(
                           power(sin(radians(st.lat - sf.lat) / 2), 2) +
                           cos(radians(sf.lat)) * cos(radians(st.lat)) *
                           power(sin(radians(st.lng - sf.lng) / 2), 2))) AS chord_m
                FROM read_parquet([{_tfiles}]) t
                JOIN sf ON sf.stop_id = t.from_stop
                JOIN st ON st.stop_id = t.to_stop
            )
            SELECT route_id,
                   SUM(dist_m) AS along_m,
                   SUM(chord_m) AS chord_m,
                   COUNT(*) AS n_eff
            FROM per
            WHERE chord_m > 0
            GROUP BY 1
            HAVING COUNT(*) >= 2
        """).df()
        for r in df.itertuples():
            if r.chord_m and r.chord_m > 0 and r.along_m and r.along_m > 0:
                detour[str(r.route_id)] = round(
                    min(2.0, max(1.0, r.along_m / r.chord_m)), 3)
        if detour:
            print(f"detour factors: {len(detour)} routes "
                  f"(median {pd.Series(list(detour.values())).median():.3f}; "
                  f"routes with <2 effective chord_m>0 edges omitted)")
        else:
            print("detour factors: none passed the >=2 effective-edge gate")
    _con.close()
except Exception as e:
    print(f"detour export skipped: {e}")

# ---- A3: real band ratios from the trained quantile cells ----
# per (route, hour): median of p10/p50 and p90/p50 across that route-hour's
# edge cells. The Worker applies them to the ride instead of the old fixed
# 0.85/1.35 multipliers, so eta bands carry the trained dispersion.
band = {}
edge_count = 0
eh = _pqt("edge_hour.parquet")
if not eh.empty:
    edge_count = int(len(eh))
    lo = (eh.p10_s / eh.p50_s).clip(0.1, 1.0)
    hi = (eh.p90_s / eh.p50_s).clip(1.0, 6.0)
    g = pd.DataFrame({"route_id": eh.route_id.astype(str),
                      "hour_ist": eh.hour_ist, "lo": lo, "hi": hi}) \
        .groupby(["route_id", "hour_ist"]).median()
    for (rid, hr), row in g.iterrows():
        band.setdefault(str(rid), {})[int(hr)] = [round(row.lo, 3), round(row.hi, 3)]
    print(f"band ratios: {len(band)} routes x hours from {edge_count:,} cells")

# ---- B7: band calibration from the journey-level holdout ----
# M.band ratios are trained per (route, hour); their realised two-sided
# coverage is measured by validation/holdout_journeys.json (`band_coverage`:
# share of journeys with pred_p10 <= ride_s <= pred_p90). A p10/p90 band
# nominally covers 80%. Rescale both sides in log-ratio space:
#   z_obs = Phi^-1((1 + cov) / 2),  z_nom = Phi^-1(0.90) ~= 1.2816
#   w     = z_nom / z_obs            (>1 widens, <1 narrows)
#   c_lo  = med_lo_ratio^(w-1)       applied by the worker: lo_ratio *= c_lo
#   c_hi  = med_hi_ratio^(w-1)       applied by the worker: hi_ratio *= c_hi
# Aggregate coverage cannot split misses between the two sides, so the
# widening/narrowing is split evenly — the neutral choice. With no usable
# holdout the factors default to the fixed [1.0, 1.0].
from statistics import NormalDist, median

BAND_TARGET_COVERAGE = 0.80   # nominal two-sided coverage of a p10/p90 band
BAND_MIN_N_SCORED = 20        # below this the coverage estimate is noise
band_cal = [1.0, 1.0]
_hj = {}
try:
    _hj_path = MODEL / "holdout_journeys.json"
    if _hj_path.exists():
        _hj = json.loads(_hj_path.read_text())
except Exception as e:
    print(f"WARNING: holdout_journeys.json unreadable ({e}) — band_cal [1, 1]")
_cov = _hj.get("band_coverage")
_n_sc = int(_hj.get("n_scored") or 0)
if isinstance(_cov, (int, float)) and not isinstance(_cov, bool) \
        and 0.0 <= float(_cov) <= 1.0:
    if _n_sc < BAND_MIN_N_SCORED:
        print(f"band_cal: band_coverage={_cov} but n_scored={_n_sc} < "
              f"{BAND_MIN_N_SCORED} — keeping [1.0, 1.0]")
    else:
        _z_nom = NormalDist().inv_cdf(0.5 * (1.0 + BAND_TARGET_COVERAGE))
        _p = min(1.0 - 1e-9, max(0.5 + 1e-9, 0.5 * (1.0 + float(_cov))))
        _z_obs = NormalDist().inv_cdf(_p)
        _w = _z_nom / _z_obs if _z_obs > 1e-6 else 3.0
        _w = min(3.0, max(0.5, _w))
        _los = [v[0] for _d in band.values() for v in _d.values()]
        _his = [v[1] for _d in band.values() for v in _d.values()]
        _med_lo = median(_los) if _los else 0.85
        _med_hi = median(_his) if _his else 1.35
        band_cal = [round(min(1.10, max(0.40, _med_lo ** (_w - 1.0))), 3),
                    round(min(2.50, max(0.87, _med_hi ** (_w - 1.0))), 3)]
        print(f"band_cal: coverage={float(_cov):.2f} vs target "
              f"{BAND_TARGET_COVERAGE:.2f} (n={_n_sc}) -> w={_w:.3f}, "
              f"band_cal={band_cal}")
else:
    print("band_cal: no usable band_coverage in holdout_journeys.json — "
          "keeping [1.0, 1.0]")

# ---- A5/C7: usual (quieter) hour per route — off-peak service-aware ----
# hours restricted to those with observed headway (a bus must actually run)
# inside the 11:00-17:00 off-peak window (§C7: NO widening outside it).
# §C7: pick the MEDIAN-speed candidate, not the fastest — "usual" must not
# cherry-pick the best hour of the window. Ties go to the earlier hour.
usual_hour = {}
wh_path = MODEL / "wait_headway_real.parquet"
wh = _pqt("wait_headway_real.parquet") if wh_path.exists() else pd.DataFrame()
wh_hours = {}
for r in wh.itertuples():
    wh_hours.setdefault(str(r.route_id), set()).add(int(r.hour_ist))
for rid in speeds:
    hrs = wh_hours.get(rid)
    if not hrs:
        continue
    # candidates must be hours the route actually runs AND has speed for;
    # never widen outside the off-peak window to night hours
    pool = [h for h in hrs if 11 <= h <= 17 and h in speeds[rid]]
    if not pool:
        continue
    _med = float(pd.Series([speeds[rid][h] for h in pool]).median())
    usual_hour[rid] = min(pool, key=lambda h: (abs(speeds[rid][h] - _med), h))

headway = {}
# B5: route -> hour -> [mean_gap_s, gap_cv] over observed consecutive
# departures (04g). The first element is the RAW mean gap — the consumer
# applies the arrival-theorem wait E[g^2]/(2E[g]) = mean*(1+cv^2)/2 and its
# own clamp, exactly like api/app.py wait_minutes() does with the parquet
# columns (wait_mean_s + gap_cv there are the same raw values).
wait_gap = {}
if wh_path.exists():
    for r in wh.itertuples():
        headway.setdefault(str(r.route_id), {})[int(r.hour_ist)] = round(
            float(r.headway_s), 1)
    if {"gap_mean_s", "gap_cv"} <= set(wh.columns):
        for r in wh.itertuples():
            if pd.isna(r.gap_mean_s) or pd.isna(r.gap_cv):
                continue
            _gm, _gcv = float(r.gap_mean_s), float(r.gap_cv)
            if _gm <= 0:
                continue
            wait_gap.setdefault(str(r.route_id), {})[int(r.hour_ist)] = [
                round(min(3600.0, _gm), 1), round(min(5.0, _gcv), 3)]
    else:
        print("WARNING: wait_headway_real.parquet lacks gap_mean_s/gap_cv — "
              "re-run pipeline/04g_headways.py; M.wait_gap stays empty")

model = {
    "patterns": patterns_out,
    "membership": member_out,
    "stops": stops_out,
    "route_names": _rn,
    "speeds": speeds,
    "speeds_n": speeds_n,
    "route_avg": route_avg,
    "route_avg_n": route_avg_n,
    "city_hour": city_hr,
    "headway": headway,
    "wait_gap": wait_gap,
    "detour": detour,
    "band": band,
    "band_cal": band_cal,
    "usual_hour": usual_hour,
    "model_source": model_source,
    "edge_count": edge_count,
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
if SIZE_MB > 25.0:
    print(f"WARNING: model.json is {SIZE_MB:.1f} MB — over the 25 MB budget")
print(f"patterns: {len(patterns_out)}, stops: {len(stops_out)},")
print(f"  membership: {len(member_out)}, speeds: {len(speeds)},")
print(f"  headways: {len(headway)} routes x hours, "
      f"wait_gap: {len(wait_gap)} routes x hours,")
print(f"  speeds_n: {len(speeds_n)} routes, route_avg_n: {len(route_avg_n)}, "
      f"usual_hour: {len(usual_hour)},")
print(f"  band_cal: {band_cal}, model_source: {model_source}")
