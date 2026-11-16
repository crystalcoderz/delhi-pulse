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
import os
import re
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()   # .env: GTFS_RT_URL / keys etc (never committed)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from rapidfuzz import process as fuzz_process

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, GTFS, MODEL, ROOT, WEB  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WALK_KMH = 4.8
WALK_ROUTE_FACTOR = 1.30
DEFAULT_WAIT_S = 300.0          # 5 min flat until headway model lands
MAX_STOPS_NEAR = 5
NEAR_KM = 2.5
CACHE_MAX = 64                  # E4: bound on replay caches (days/windows)
DAY_RE = re.compile(r"^\d{4}_\d{2}_\d{2}$")
DEPART_HINT = "use ISO 8601, e.g. 2026-11-17T08:30:00+05:30"
# D4 parity with workers/worker.js: coordinate queries must look like Delhi
DELHI_BOX = (27.0, 30.0, 74.5, 79.5)   # lat_lo, lat_hi, lng_lo, lng_hi

app = FastAPI(title="Delhi Pulse", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# live layer: starts when GTFS_RT_URL is present in the environment
from .live import LIVE as LIVE_LIVE  # noqa: E402
_rt_url = os.environ.get("GTFS_RT_URL")
if _rt_url:
    LIVE_LIVE.url = _rt_url
    if os.environ.get("GTFS_RT_API_KEY"):
        LIVE_LIVE.headers = {"x-api-key": os.environ["GTFS_RT_API_KEY"]}
    LIVE_LIVE.start()

@app.get("/", include_in_schema=False)
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/web/")


# ---------------------------------------------------------------- static GTFS
stops = pd.read_csv(GTFS / "stops.txt")
stops["stop_id"] = stops["stop_id"].astype(str)          # canonical string ids
routes = pd.read_csv(GTFS / "routes.txt")
routes["route_id"] = routes["route_id"].astype(str)
routes = routes.set_index("route_id")
trips = pd.read_csv(GTFS / "trips.txt", dtype=str)
stimes = None   # loaded lazily only when the pattern cache is cold

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

import hashlib
import pickle
import time as _time

_stimes_mtime = (GTFS / "stop_times.txt").stat().st_mtime
_cache_key = hashlib.md5(str(_stimes_mtime).encode()).hexdigest()[:10]
_cache_path = MODEL / f"patterns_{_cache_key}.pkl"

_seen = {}   # (route_id, stops-tuple) -> pattern index, collapses ~89k trips to distinct patterns
MEMBER_CAP = 80   # max patterns listed per stop (real hub stops serve 100+)

if _cache_path.exists():
    with open(_cache_path, "rb") as f:
        PAT, STOP_MEMBER = pickle.load(f)
else:
    stimes = pd.read_csv(GTFS / "stop_times.txt", dtype=str)  # cold boot only
    _g = stimes.sort_values(["trip_id", "stop_sequence"]).groupby("trip_id")
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
            lst = STOP_MEMBER.setdefault(sid, [])
            if len(lst) < MEMBER_CAP:
                lst.append((pi, pos))
    MODEL.mkdir(parents=True, exist_ok=True)
    with open(_cache_path, "wb") as f:
        pickle.dump((PAT, STOP_MEMBER), f, protocol=pickle.HIGHEST_PROTOCOL)
    # opportunistic cleanup of stale caches
    for old in MODEL.glob("patterns_*.pkl"):
        if old != _cache_path:
            old.unlink()
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
headway_hour = _load("headway_hour.parquet")

E50 = {(str(r.route_id), str(r.from_stop), str(r.to_stop), int(r.hour_ist)):
       (float(r.p50_s), float(getattr(r, "p10_s", r.p50_s)),
        float(getattr(r, "p90_s", r.p50_s)))
       for r in edge_hour.itertuples()} if len(edge_hour) else {}
RH = {(str(r.route_id), int(r.hour_ist)): float(r.speed_kmh)
      for r in route_hour.itertuples()} if len(route_hour) else {}
RS = {str(r.route_id): float(r.speed_kmh) for r in route_speed.itertuples()} if len(route_speed) else {}
CH = {int(r.hour_ist): r.speed_kmh for r in city_hour.itertuples()} if len(city_hour) else {}
CH_DEFAULT = float(np.median(list(CH.values()))) if CH else 15.0

# ---- REAL ping-derived network speeds (pre-GTFS; take precedence) ----
def _load_real(name):
    p = MODEL / name
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()

_rh_real = _load_real("route_hour_real.parquet")
_rs_real = _load_real("route_speed_real.parquet")
_ch_real = _load_real("city_hour_real.parquet")
RHR = {(str(r.route_id), int(r.hour_ist)): float(r.speed_kmh)
       for r in _rh_real.itertuples()} if len(_rh_real) else {}
RSR = {str(r.route_id): float(r.speed_kmh)
       for r in _rs_real.itertuples()} if len(_rs_real) else {}
CHR = {int(r.hour_ist): float(r.speed_kmh)
       for r in _ch_real.itertuples()} if len(_ch_real) else {}
_rhd_real = _load_real("route_hour_dow_real.parquet")
USE_DOW = False   # flip to True when hold_pings shows the dow arm beating pooled
DRHR = {(str(r.route_id), int(r.dow_ist), int(r.hour_ist)): float(r.speed_kmh)
        for r in _rhd_real.itertuples()} if len(_rhd_real) else {}
if CHR:
    CH_DEFAULT = float(np.median(list(CHR.values())))
WAIT50 = {(str(r.route_id), str(r.stop_id)): float(r.h50_s)
          for r in headway.itertuples()} if len(headway) else {}
WAIT50H = {(str(r.route_id), str(r.stop_id), int(getattr(r, "h_ist", 0))): float(r.h50_s)
           for r in headway_hour.itertuples()} if len(headway_hour) else {}
_wait_real_df = _load_real("wait_headway_real.parquet")
WAITR = {(str(r.route_id), int(r.hour_ist)): float(r.headway_s)
         for r in _wait_real_df.itertuples()} if len(_wait_real_df) else {}

# ---- B5: observed-gap wait model ----
# Half the headway assumes perfectly even spacing. The arrival-theorem wait for
# a renewal process is E[g^2] / (2 E[g]) = mean * (1 + cv^2) / 2, so the gap
# spread matters as much as the mean. The 04g/export agent is adding
# `wait_mean_s` + `gap_cv` to wait_headway_real.parquet (and M.wait_gap).
# Until those columns exist this map is empty and the headway/2 model below is
# used unchanged.
# TODO(B5): drop the headway/2 branch once the exporter writes wait_mean_s and
# gap_cv (per route-hour) — then waits carry the observed dispersion.
# 04g stores the RAW mean gap (wait_mean_s / gap_mean_s are aliases) and the
# gap coefficient of variation; the arrival-theorem correction is applied here.
WAITGAP = {}
if len(_wait_real_df) and "gap_cv" in _wait_real_df.columns:
    _mean_col = next((c for c in ("wait_mean_s", "gap_mean_s")
                      if c in _wait_real_df.columns), None)
    if _mean_col:
        WAITGAP = {(str(r.route_id), int(r.hour_ist)):
                   (min(3600.0, max(0.0, float(getattr(r, _mean_col)))),
                    min(5.0, max(0.0, float(r.gap_cv))))
                   for r in _wait_real_df.itertuples()
                   if pd.notna(getattr(r, _mean_col)) and pd.notna(r.gap_cv)}


def wait_minutes(route_id, stop_id, hour=None):
    """Expected wait, clamped [0.75, 15] min.
    Precedence: observed gaps (B5, once exported) -> real service headways
    (trip labels) -> synth stop headways (pooled, hour-aware) -> flat default."""
    route_id, stop_id = str(route_id), str(stop_id)
    if hour is not None:
        gp = WAITGAP.get((route_id, int(hour) % 24))
        if gp:
            mean_s, cv = gp
            return min(15.0, max(0.75, mean_s * (1.0 + cv * cv) / 2.0 / 60.0))
        h = WAITR.get((route_id, int(hour) % 24))
        if h:
            return min(15.0, max(0.75, h / 120.0))
        h = WAIT50H.get((route_id, stop_id, int(hour) % 24))
        if h:
            return min(15.0, max(0.75, h / 120.0))
    h = WAIT50.get((route_id, stop_id))
    if h:
        return min(15.0, max(0.75, h / 120.0))
    return DEFAULT_WAIT_S / 60.0


# (old ride_time superseded by the day-aware version above)


# ---- C7: per-route usual (off-peak) hour ----
# Mirrors the exporter rule (deployment/export_model_json.py, C7) exactly:
# candidates are hours 11..17 that have BOTH a speed (route_hour.parquet, with
# the ping-pair table only as the exporter's whole-table fallback) and an
# observed headway (a bus actually runs). The "usual" hour is the candidate
# whose speed is the MEDIAN of the pool — not the fastest — with ties going to
# the earlier hour. No widening to the night hours the old 6/7/22/23 sampler
# used.
def _build_usual_hours():
    speed_by_route = {}
    src = RH if RH else RHR          # same table choice as the exporter
    for (rid, h), sp in src.items():
        speed_by_route.setdefault(rid, {})[h] = round(float(sp), 2)
    head_hours = {}
    if WAITR:                              # real service headways
        for (rid, _h) in WAITR:
            head_hours.setdefault(rid, set()).add(_h)
    else:                                  # synth hourly headways, else any
        for (rid, _sid, _h) in WAIT50H:
            head_hours.setdefault(rid, set()).add(_h)
        if not head_hours and not WAIT50:
            head_hours = {rid: set(range(24)) for rid in speed_by_route}
    out = {}
    for rid, hrs in head_hours.items():
        sp = speed_by_route.get(rid)
        if not sp:
            continue
        pool = [h for h in hrs if 11 <= h <= 17 and h in sp]
        if not pool:
            continue
        _med = float(np.median([sp[h] for h in pool]))
        out[rid] = int(min(pool, key=lambda h: (abs(sp[h] - _med), h)))
    return out


USUAL_HOUR = _build_usual_hours()


# ---- E4: request input validation ----
# `day` / `asOf` end up in file paths and cache keys, so only well-formed
# names that /days actually lists are accepted.
_KNOWN_DAYS = None
_KNOWN_DAYS_AT = 0.0


def known_days():
    """Days the archive can serve: traversal/ping-pair speed days plus
    ping-only replay days. Cached for 60 s; same set /days reports."""
    global _KNOWN_DAYS, _KNOWN_DAYS_AT
    now = _time.monotonic()
    if _KNOWN_DAYS is None or now - _KNOWN_DAYS_AT > 60.0:
        _KNOWN_DAYS = (
            {p.stem.replace("traversals_", "")
             for p in CLEAN.glob("traversals_*.parquet")}
            | {p.stem.replace("speeds_", "")
               for p in CLEAN.glob("speeds_*.parquet")}
            | {p.stem.replace("pings_", "")
               for p in CLEAN.glob("pings_*.parquet")}
        )
        _KNOWN_DAYS_AT = now
    return _KNOWN_DAYS


def _require_day(strday, param="day"):
    """Reject anything that is not YYYY_MM_DD or not in /days (E4)."""
    if not DAY_RE.match(strday or ""):
        raise HTTPException(400, detail=f"{param} must be a YYYY_MM_DD archive "
                                        f"day (see /days)")
    if strday not in known_days():
        raise HTTPException(400, detail=f"{param}: unknown archive day "
                                        f'"{strday}" (see /days)')


def _in_delhi(lat, lng):
    la_lo, la_hi, ln_lo, ln_hi = DELHI_BOX
    return la_lo < lat < la_hi and ln_lo < lng < ln_hi


def _parse_depart(s):
    """Structural depart parse — same accepted forms and error text as the
    Worker (workers/worker.js D1): ISO 8601 with optional offset, a space
    separator, or a bare date (midnight IST). Garbage -> 400 + hint."""
    try:
        return dt.datetime.fromisoformat(s.strip())
    except ValueError:
        raise HTTPException(400, detail=(
            f'depart cannot be parsed: "{s}" (hint: {DEPART_HINT})'))


def _cache_put(cache, key, value):
    """Bounded insert: oldest entry is evicted once the cache hits 64 (E4)."""
    if key not in cache and len(cache) >= CACHE_MAX:
        try:
            cache.pop(next(iter(cache)))
        except StopIteration:
            pass
    cache[key] = value


def _speed_chain(route_id, hour, day=None, dow=None):
    """Historical (route,hour) speed, before any live/window blending."""
    if day is not None:
        drh, dravg = day
        return (drh.get((route_id, hour)) or dravg.get(route_id)
                or RH.get((route_id, hour)) or RS.get(route_id)
                or RHR.get((route_id, hour)) or RSR.get(route_id)
                or CHR.get(hour) or CH.get(hour) or CH_DEFAULT)
    if dow is not None and USE_DOW:
        return (DRHR.get((route_id, dow, hour))
                or RH.get((route_id, hour)) or RS.get(route_id)
                or RHR.get((route_id, hour)) or RSR.get(route_id)
                or CHR.get(hour) or CH.get(hour) or CH_DEFAULT)
    # B2: traversal speeds (dwell-inclusive, along-route) BEFORE the
    # moving-only ping-pair tables
    return (RH.get((route_id, hour)) or RS.get(route_id)
            or RHR.get((route_id, hour)) or RSR.get(route_id)
            or CHR.get(hour) or CH.get(hour) or CH_DEFAULT)


def _blend_window(route_id, dist_m, hist_s, win):
    """B3-lite: weigh an observed window speed against the historical chain.

    The window is a RATIO on the historical timing (0.5..2.0) rather than a
    replacement, weighted by its pair count: alpha = min(0.8, n/(n+30)). The
    blend happens in speed space, so a fast 3-pair window can no longer time a
    whole route on three pings.
    """
    if not win or hist_s <= 0:
        return hist_s
    obs = win.get(route_id)
    if not obs:
        return hist_s
    wsp, n = obs
    if not wsp or wsp <= 0 or not n:
        return hist_s
    hist_sp = dist_m * 3.6 / hist_s
    if hist_sp <= 0:
        return hist_s
    ratio = min(2.0, max(0.5, float(wsp) / hist_sp))
    alpha = min(0.8, float(n) / (float(n) + 30.0))
    return hist_s / (1.0 + alpha * (ratio - 1.0))


def leg_seconds(route_id, a, b, dist_m, hour, day=None, win=None, dow=None):
    """Median traversal seconds for one leg. DOW-aware speed first; a live or
    replay window is blended in as a clamped, pair-count-weighted ratio."""
    e = E50.get((route_id, a, b, hour))
    if e and day is None:
        hist_s = e[0]
    else:
        sp = _speed_chain(route_id, hour, day=day, dow=dow)
        hist_s = dist_m * 3.6 / max(1.0, sp)   # meters at km/h -> seconds
    return _blend_window(route_id, dist_m, hist_s, win)


_DAY_SPEED_CACHE = {}


def _day_speeds(day):
    """Observed (route,hour)->speed for one day. Lazy dict; empty if absent.
    Prefers traversals (stop-visit based); falls back to ping-pair speeds
    (works for real days with no GTFS yet)."""
    if day not in _DAY_SPEED_CACHE:
        import duckdb
        tpath = CLEAN / f"traversals_{day}.parquet"
        spath = CLEAN / f"speeds_{day}.parquet"
        apath = CLEAN / f"speedsagg_{day}.parquet"
        try:
            if tpath.exists():
                con = duckdb.connect()
                df = con.execute(f"""
                    SELECT route_id, hour_ist,
                           SUM(dist_m) / NULLIF(SUM(dur_s), 0) * 3.6 AS sp FROM (
                        SELECT *, COUNT(*) OVER (PARTITION BY route_id, hour_ist) n
                        FROM read_parquet('{tpath.as_posix()}'))
                    GROUP BY 1, 2 HAVING MIN(n) >= 2
                """).df()
                df2 = con.execute(f"""
                    SELECT route_id,
                           SUM(dist_m) / NULLIF(SUM(dur_s), 0) * 3.6 AS sp
                    FROM read_parquet('{tpath.as_posix()}') GROUP BY 1
                """).df()
                con.close()
            elif spath.exists():
                df = pd.read_parquet(spath)
                df = df[df.n >= 4]
                df2 = pd.read_parquet(apath) if apath.exists() else pd.DataFrame()
            else:
                df = pd.DataFrame()
                df2 = pd.DataFrame()
            _cache_put(_DAY_SPEED_CACHE, day, (
                {(str(r.route_id), int(r.hour_ist)): float(r.sp)
                 for r in df.itertuples()},
                {str(r.route_id): float(r.sp)
                 for r in df2.itertuples()} if len(df2) else {},
            ))
        except Exception:
            _cache_put(_DAY_SPEED_CACHE, day, ({}, {}))
    return _DAY_SPEED_CACHE[day]


_WINDOW_CACHE = {}


def _window_speeds(day, end_epoch, window_s=2700.0, min_pairs=4):
    """Per-route implied speeds + pair counts from ping pairs inside
    [end-window, end]. This is the replay twin of
    LiveLayer.window_speeds() — same computation, archive slice instead of
    live store. Cached in 15-min buckets (bounded at CACHE_MAX)."""
    key = (day, int(end_epoch // 900), int(window_s))
    if key in _WINDOW_CACHE:
        return _WINDOW_CACHE[key]
    path = CLEAN / f"pings_{day}.parquet"
    if not path.exists():
        _cache_put(_WINDOW_CACHE, key, {})
        return {}
    import duckdb
    con = duckdb.connect()
    df = con.execute(f"""
        WITH seq AS (
            SELECT route_id, lat, lng, ts_utc,
                   lead(lat) OVER w AS nlat,
                   lead(lng) OVER w AS nlng,
                   lead(ts_utc) OVER w AS nts
            FROM read_parquet('{path.as_posix()}')
            WHERE ts_utc BETWEEN {int(end_epoch - window_s)} AND {int(end_epoch)}
            WINDOW w AS (PARTITION BY trip_id, vehicle_id ORDER BY ts_utc)
        )
        SELECT route_id,
               SUM(sqrt(power((nlng-lng)*111320.0*cos(radians((nlat+lat)/2.0)),2)
                        + power((nlat-lat)*110540.0,2) )) AS dist_sum,
               SUM(nts - ts_utc) AS dt_sum,
               COUNT(*) AS n
        FROM seq
        WHERE nts > ts_utc AND nts - ts_utc <= 120
        GROUP BY 1 HAVING COUNT(*) >= {min_pairs}
    """).df()
    con.close()
    out = {str(r.route_id): (float(r.dist_sum / max(1e-9, r.dt_sum) * 3.6),
                             int(r.n))
           for r in df.itertuples()}
    _cache_put(_WINDOW_CACHE, key, out)
    return out


def ride_time(pat, i, j, start_epoch, day=None, win=None):
    """Sum leg seconds i..j with hour refinement.
    Returns (total_s, end_epoch, p10_s, p90_s)."""
    t = start_epoch
    total = 0.0
    lo = hi = 0.0
    for k in range(i, j):
        h = int((t + 19800) // 3600 % 24)  # IST hour at leg start
        dw = ((int(t // 86400) + 3) % 7) + 1  # isodow (1=Mon..7=Sun)
        a, b = pat["stops"][k], pat["stops"][k + 1]
        dist = pat["leg_m"][k]
        e = (E50.get((pat["route_id"], a, b, h)) if (day is None and win is None)
             else None)
        s = leg_seconds(pat["route_id"], a, b, dist, h, day=day, win=win, dow=dw)
        total += s
        t += s
        if e:
            lo += e[1]
            hi += e[2]
        else:
            lo += s * 0.85
            hi += s * 1.35
    return total, t, lo, hi


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


@app.get("/stops/nearby")
def stops_nearby(lat: float, lng: float, radius_m: float = 500):
    """Find stops within a radius (default 500m) of a point."""
    from math import cos, radians
    r_lat = radians(lat)
    d = np.sqrt(((_slng - lng) * 111320.0 * r_lat) ** 2 +
                ((_slat - lat) * 110540.0) ** 2)
    idx = np.argsort(d)[:8]
    out = []
    for i in idx:
        if d[i] <= radius_m:
            sid = stops.stop_id.iloc[i]
            out.append(dict(stop_id=sid, name=stops.stop_name.iloc[i],
                            lat=float(_slat[i]), lng=float(_slng[i]),
                            distance_m=int(d[i])))
    return out


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
             depart: str | None = None,
             asOf: str | None = None,
             max_options: int = Query(3, alias="max", ge=1, le=8)):
    o = resolve_place(fromq)
    d = resolve_place(toq)
    # E4/D4: refuse coordinates nowhere near Delhi (same message as the Worker)
    for p in (o, d):
        if not _in_delhi(p["lat"], p["lng"]):
            raise HTTPException(400, detail="coordinates are outside the Delhi region")
    dep = _parse_depart(depart) if depart else dt.datetime.now(IST)
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=IST)
    dep = dep.astimezone(IST)
    day = None
    win = None
    replay_mode = None
    strday = None
    if asOf:
        try:
            t = dt.datetime.fromisoformat(asOf.strip())
        except ValueError:
            raise HTTPException(400, detail=(
                f'asOf cannot be parsed: "{asOf}" '
                '(hint: use YYYY-MM-DD or ISO 8601)'))
        strday = f"{t.year:04d}_{t.month:02d}_{t.day:02d}"
        _require_day(strday, "asOf")
        if t.tzinfo is None:      # bare local asOf = IST (Worker parity)
            t = t.replace(tzinfo=IST)
        day = _day_speeds(strday)
        if t.hour or t.minute or t.second:
            win = _window_speeds(strday, t.timestamp(), window_s=2700)
            replay_mode = f"window@{asOf}"
        else:
            replay_mode = f"day@{strday}"
    elif LIVE_LIVE.status()["url"]:
        wl = LIVE_LIVE.window_speeds(window_s=900)
        if wl:
            win = dict(wl)          # B3-lite: keep (speed, pairs) for blending
            replay_mode = "live"

    o_near = nearest_stops(o["lat"], o["lng"]) or \
        nearest_stops(o["lat"], o["lng"], k=5, radius_km=6)
    d_near = nearest_stops(d["lat"], d["lng"]) or \
        nearest_stops(d["lat"], d["lng"], k=5, radius_km=6)
    if not o_near or not d_near:
        raise HTTPException(404, "no plausible stop access found")

    def route_name(rid):
        return routes_short.get(str(rid), str(rid))

    TRANSFER_BUFFER_S = 120.0
    MAX_OPTIONS = 400        # hard cap: worst-case queries stay sub-second
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
                    if len(options) >= MAX_OPTIONS:
                        break
                    bhour = int((dep_epoch + oa["walk_min"] * 60 + 19800) // 3600 % 24)
                    wait_a = wait_minutes(pat["route_id"], oa["stop_id"], hour=bhour)
                    board_epoch = dep_epoch + oa["walk_min"] * 60 + wait_a * 60
                    ride_f, _, ride_lo, ride_hi = ride_time(
                        pat, i, j, board_epoch, day=day, win=win)
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
                        _geo=("direct", pat, i, j, oa, db_),
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
                                          + ride_lo / 60, 1),
                        eta_p90_min=round(oa["walk_min"] + wait_a
                                          + ride_hi / 60, 1),
                        usual_p50_min=round(total, 1),
                        confidence="v0.5-transfers",
                    ))

    # Build per-request walk-neighbor cache for transfer points (<=350 m)
    xfer_cache = {}

    def xfer_neighbors(stop_id):
        if stop_id not in xfer_cache:
            la, lo, _ = stop_pos[stop_id]
            ns = nearest_stops(la, lo, k=5, radius_km=0.35)
            xfer_cache[stop_id] = [
                (n["stop_id"], n["walk_min"], n["name"], n["lat"], n["lng"])
                for n in ns if n["walk_min"] <= 12]
        return xfer_cache[stop_id]

    def _finish(opts):
        if not opts:
            return dict(options=[],
                        note="no route found via max 1 transfer in range",
                        replay_day=strday, replay_mode=replay_mode)
        opts.sort(key=lambda x: x["total_min"])
        seen, picked = set(), []
        for op in opts:
            if op["_key"] in seen:
                continue
            seen.add(op["_key"])
            op.pop("_key")
            geo = op.pop("_geo", None)
            if geo:
                u = _usual_at_quiet_hour(geo)
                if u:
                    op["usual_p50_min"] = u["total_min"]
                    op["usual_hour_ist"] = u["hour"]
                    op["rush_effect_min"] = round(
                        op["total_min"] - u["total_min"], 1)
                    op["usual_wait_min"] = u["wait_min"]
            picked.append(op)
            if len(picked) >= max_options:
                break
        return dict(options=picked, replay_day=strday, replay_mode=replay_mode)

    def _usual_at_quiet_hour(geo):
        """C7: the same journey timed at each route's off-peak MEDIAN hour
        (11:00-17:00, present in both the speed layer and the observed
        headways — the exporter's rule, no night-hour widening). Returns None
        when a leg's route has no qualifying hour."""
        if geo[0] == "direct":
            _, pat, i, j, oa, db_ = geo
            uh = USUAL_HOUR.get(str(pat["route_id"]))
            if uh is None:
                return None
            quiet_ts = dep.replace(hour=uh, minute=30, second=0,
                                   microsecond=0).timestamp()
            wait_q = wait_minutes(pat["route_id"], oa["stop_id"], hour=uh)
            board_q = quiet_ts + oa["walk_min"] * 60 + wait_q * 60
            ride_q, _, _, _ = ride_time(pat, i, j, board_q)
            total = (oa["walk_min"] + wait_q + ride_q / 60 + db_["walk_min"])
            return dict(total_min=round(total, 1), hour=uh,
                        wait_min=round(wait_q, 1))
        _, A, i, ia, B, tb, j, oa, db_, tstop, wmin = geo
        uhA = USUAL_HOUR.get(str(A["route_id"]))
        uhB = USUAL_HOUR.get(str(B["route_id"]))
        if uhA is None or uhB is None:
            return None
        ts_a = dep.replace(hour=uhA, minute=30, second=0,
                           microsecond=0).timestamp()
        wait_a = wait_minutes(A["route_id"], oa["stop_id"], hour=uhA)
        board_q = ts_a + oa["walk_min"] * 60 + wait_a * 60
        rideA, _, _, _ = ride_time(A, i, ia, board_q)
        wait_b = wait_minutes(B["route_id"], tstop, hour=uhB)
        xfer_s = TRANSFER_BUFFER_S + wmin * 60 + wait_b * 60
        ts_b = dep.replace(hour=uhB, minute=30, second=0,
                           microsecond=0).timestamp()
        rideB, _, _, _ = ride_time(B, tb, j, ts_b + xfer_s)
        total = (oa["walk_min"] + wait_a + rideA / 60 + xfer_s / 60
                 + rideB / 60 + db_["walk_min"])
        return dict(total_min=round(total, 1), hour=min(uhA, uhB),
                    wait_min=round(wait_a + xfer_s / 60, 1))

    def _emit_transfer(A, B, i, ia, tb, j, oa, tstop, wmin, db_, via=None):
        if len(options) >= MAX_OPTIONS:
            return
        bhour = int((dep_epoch + oa["walk_min"] * 60 + 19800) // 3600 % 24)
        wait_a = wait_minutes(A["route_id"], oa["stop_id"], hour=bhour)
        board_epoch = dep_epoch + oa["walk_min"] * 60 + wait_a * 60
        rideA, t_at_tr, loA, hiA = ride_time(A, i, ia, board_epoch, day=day, win=win)
        thour = int((t_at_tr + 19800) // 3600 % 24)
        wait_b = wait_minutes(B["route_id"], tstop, hour=thour)
        xfer_s = TRANSFER_BUFFER_S + wmin * 60 + wait_b * 60
        t_board2 = t_at_tr + xfer_s
        rideB, _, loB, hiB = ride_time(B, tb, j, t_board2, day=day, win=win)
        total = (oa["walk_min"] + wait_a + rideA / 60 + xfer_s / 60
                 + rideB / 60 + db_["walk_min"])
        tname = stop_pos[tstop][2] if tstop in stop_pos else tstop
        options.append(dict(
            _key=(A["route_id"], B["route_id"], tstop),
            transfers=1,
            total_min=round(total, 1),
            walk_o=oa["walk_min"], walk_d=db_["walk_min"],
            wait_min=round(wait_a + xfer_s / 60, 1),
            ride_min=round((rideA + rideB) / 60, 1),
            route_id=f"{A['route_id']}+{B['route_id']}",
            route_name=f"{route_name(A['route_id'])} + {route_name(B['route_id'])}",
            board=oa["stop_id"], board_name=oa["name"],
            alight=db_["stop_id"], alight_name=db_["name"],
            transfer_stop=tstop, transfer_name=tname,
            transfer_walk_min=round(wmin, 1) if wmin else None,
            transfer_via=via,
            transfer_coord=[stop_pos[tstop][1], stop_pos[tstop][0]]
                if tstop in stop_pos else None,
            board_at=(dep + dt.timedelta(
                minutes=oa["walk_min"] + wait_a)).isoformat(),
            arrive=(dep + dt.timedelta(minutes=total)).isoformat(),
            _geo=("xfer", A, i, ia, B, tb, j, oa, db_, tstop, wmin),
            legs=[
                dict(type="walk", minutes=oa["walk_min"],
                     coords=[[o["lat"], o["lng"]], [oa["lat"], oa["lng"]]]),
                dict(type="bus", minutes=round(rideA / 60, 1),
                     route=route_name(A["route_id"]),
                     stops=_bus_leg_coords(A, i, ia)),
                dict(type="transfer", minutes=round(xfer_s / 60, 1),
                     at=tstop, walk_min=round(wmin, 1) if wmin else None),
                dict(type="bus", minutes=round(rideB / 60, 1),
                     route=route_name(B["route_id"]),
                     stops=_bus_leg_coords(B, tb, j)),
                dict(type="walk", minutes=db_["walk_min"],
                     coords=[[db_["lat"], db_["lng"]], [d["lat"], d["lng"]]]),
            ],
            eta_p50_min=round(total, 1),
            eta_p10_min=round(oa["walk_min"] + wait_a + xfer_s / 60
                              + (loA + loB) / 60, 1),
            eta_p90_min=round(oa["walk_min"] + wait_a + xfer_s / 60
                              + (hiA + hiB) / 60, 1),
            usual_p50_min=round(total, 1),
            confidence="v0.6-walk-transfers",
        ))

    # ---------------- one-transfer options (same-stop + walk transfers) ----
    for oa in o_near:
        for db_ in d_near:
            for pi, i in STOP_MEMBER.get(oa["stop_id"], []):
                A = PAT[pi]
                apos_after = [(s, k) for k, s in enumerate(A["stops"]) if k > i]
                if not apos_after:
                    continue
                for pj, j in STOP_MEMBER.get(db_["stop_id"], []):
                    B = PAT[pj]
                    if pi == pj or A["route_id"] == B["route_id"]:
                        continue
                    bpos = {s: k for k, s in enumerate(B["stops"])}
                    for tstop, ia in apos_after:
                        tb = bpos.get(tstop)
                        if tb is None or tb >= j:
                            continue
                        _emit_transfer(A, B, i, ia, tb, j, oa, tstop, 0.0, db_)
                        if len(options) >= MAX_OPTIONS:
                            return _finish(options)
                        # walk transfers (<=350 m hop between stops)
                        for (t2, wmin, wname, wlat, wlng) in xfer_neighbors(tstop):
                            if t2 == tstop:
                                continue
                            tb2 = bpos.get(t2)
                            if tb2 is None or tb2 >= j:
                                continue
                            _emit_transfer(A, B, i, ia, tb2, j, oa, t2, wmin,
                                           db_, via=(tstop,))
                            if len(options) >= MAX_OPTIONS:
                                return _finish(options)
    return _finish(options)


@app.get("/pulse")
def pulse():
    src = CHR if CHR else CH
    return dict(city_hour={str(k): round(v, 2) for k, v in sorted(src.items())},
                source="real-ping-pairs" if CHR else "synthetic",
                note="hour -> observed mean network speed km/h")


# ---------------- Delhi Pulse traffic overlay ----------------
_SHAPES = {}
_shp = pd.read_csv(GTFS / "shapes.txt")
for sid, g in _shp.groupby("shape_id", sort=False):
    g = g.sort_values("shape_pt_sequence")
    _SHAPES[str(sid)] = list(zip(g.shape_pt_lat.astype(float),
                                  g.shape_pt_lon.astype(float)))
_ROUTE_SHAPES = {}
for r in trips.itertuples():
    _ROUTE_SHAPES.setdefault(str(r.route_id), str(r.shape_id))


def _ramp(kmh, vmax=36.0):
    """Speed -> color: slow amber/red, fast teal (cividis-like, dark bg)."""
    t = max(0.0, min(1.0, kmh / vmax))
    # slow: (240,110,40) fast: (60,210,160)
    r = int(240 + (60 - 240) * t)
    g = int(110 + (210 - 110) * t)
    b = int(40 + (160 - 40) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


@app.get("/geo")
def geo(hour: int = 9, day: str | None = None, limit: int = 800,
        stride: int = 3):
    if day:
        _require_day(day, "day")     # E4: no arbitrary day -> file path
    drh, dravg = _day_speeds(day) if day else ({}, {})
    feats = []
    # order routes by ping-pair coverage so the visible set is the busiest
    ranked = sorted(_ROUTE_SHAPES.keys(),
                    key=lambda r: -RHR.get((r, hour), RSR.get(r, 0) or 0))
    for rid in ranked[:max(10, min(limit, 2500))]:
        sid = _ROUTE_SHAPES[rid]
        pts = _SHAPES.get(sid)
        if not pts:
            continue
        sp = (drh.get((rid, hour)) or dravg.get(rid)
              or RH.get((rid, hour)) or RS.get(rid) or CH.get(hour) or CH_DEFAULT)
        coords = [[round(lng, 5), round(lat, 5)]
                  for lat, lng in pts[::max(1, stride)]]
        if len(coords) >= 2:
            feats.append({
                "type": "Feature",
                "properties": {"route": rid,
                               "name": routes_short.get(rid, rid),
                               "speed_kmh": round(float(sp), 1),
                               "color": _ramp(float(sp))},
                "geometry": {"type": "LineString", "coordinates": coords},
            })
    return {"type": "FeatureCollection", "features": feats,
            "hour": hour, "day": day}


@app.get("/days")
def days():
    # the same set _require_day() enforces (speeds days + ping-only replay days)
    return {"days": sorted(known_days())}


@app.get("/status")
def status():
    pats = len(PAT)
    live = LIVE_LIVE.status()
    try:
        holdout = json.loads((MODEL / "holdout_report.json").read_text())
    except Exception:
        holdout = None
    try:
        meta = json.loads((MODEL / "meta.json").read_text())
    except Exception:
        meta = None
    return dict(ok=True, patterns=pats, edges=len(E50), routes=len(RS),
                live=live, holdout=holdout, model=meta,
                replay_days=sorted({p.stem.replace("pings_", "")
                                     for p in CLEAN.glob("pings_*.parquet")}))


if (ROOT / "web").exists():
    app.mount("/web", StaticFiles(directory=str(WEB), html=True), name="web")
