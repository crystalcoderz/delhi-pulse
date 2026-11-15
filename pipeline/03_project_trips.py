"""
Stage 3: project clean pings onto GTFS shapes -> stop passages -> edge traversals.

Modes (auto-detected):
  trips   — GPS trip_id joins static GTFS (synthetic fixture / aligned feeds)
  routes  — trip_id is a runtime label ("1795_19_31"); key on route_id, try the
            route's shapes (both directions), keep the best fit; segment
            trajectories at ping gaps >10 min.

Parallel: --workers N (default: cpu count//2). Workers load the GTFS once
each; the parent streams (trip, vehicle) trajectories in batches.

Outputs per day:
  data/clean/passages_YYYY_MM_DD.parquet
  data/clean/traversals_YYYY_MM_DD.parquet
"""
import argparse
import math
import multiprocessing as mp
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from common import (ensure_dirs, CLEAN, GTFS,
                    NEAR_SHAPE_M, NEAR_STOP_M, MIN_TRIP_PINGS,
                    MIN_EDGE_S, MAX_EDGE_S)

C_LAT, C_LNG = 28.62, 77.21
K_LAT, K_LNG = 110540.0, 111320.0 * math.cos(math.radians(C_LAT))
SEGMENT_GAP_S = 600.0
BATCH_TRAJECTORIES = 200


class ShapeCache:
    def __init__(self):
        self.shapes = {}

    def load(self):
        shp = pd.read_csv(GTFS / "shapes.txt")
        for sid, g in shp.groupby("shape_id", sort=False):
            g = g.sort_values("shape_pt_sequence")
            lat = g.shape_pt_lat.to_numpy(float)
            lng = g.shape_pt_lon.to_numpy(float)
            x = (lng - C_LNG) * K_LNG
            y = (lat - C_LAT) * K_LAT
            cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
            self.shapes[str(sid)] = (x, y, cum)
        return self

    def get(self, shape_id):
        return self.shapes.get(shape_id)


def project_to_polyline(px, py, x, y, cum, chunk=256):
    ax, ay = x[:-1], y[:-1]
    bx, by = x[1:], y[1:]
    vx, vy = bx - ax, by - ay
    seg_len2 = vx * vx + vy * vy
    seg_len2 = np.where(seg_len2 < 1e-9, 1e-9, seg_len2)
    n = len(px)
    s_out = np.empty(n)
    d_out = np.empty(n)
    for i in range(0, n, chunk):
        qx = px[i:i + chunk][:, None]
        qy = py[i:i + chunk][:, None]
        t = ((qx - ax) * vx + (qy - ay) * vy) / seg_len2
        t = np.clip(t, 0.0, 1.0)
        nx = ax + t * vx
        ny = ay + t * vy
        d2 = (qx - nx) ** 2 + (qy - ny) ** 2
        j = np.argmin(d2, axis=1)
        r = np.arange(d2.shape[0])
        d_out[i:i + chunk] = np.sqrt(d2[r, j])
        s_out[i:i + chunk] = cum[j] + t[r, j] * np.sqrt(seg_len2[j])
    return s_out, d_out


def passages_for_segment(s, d, t_, seg_mask, shape, ref_stops, stops_xy,
                         trip_id, vehicle_id, route_id, passages, traversals):
    x, y, cum = shape
    ssel = s[seg_mask]
    tsel = t_[seg_mask]
    if len(ssel) < MIN_TRIP_PINGS:
        return 0
    s_mon = np.maximum.accumulate(ssel)
    if ref_stops is None:
        return 0
    sx = (stops_xy.loc[ref_stops.stop_id, "stop_lon"].to_numpy() - C_LNG) * K_LNG
    sy = (stops_xy.loc[ref_stops.stop_id, "stop_lat"].to_numpy() - C_LAT) * K_LAT
    s_stop, sd = project_to_polyline(sx, sy, x, y, cum)
    good = sd <= NEAR_STOP_M
    ev = []
    prev_s = -1.0
    for i in range(len(ref_stops)):
        if not good[i] or s_stop[i] <= prev_s:
            continue
        prev_s = s_stop[i]
        j = np.searchsorted(s_mon, s_stop[i])
        if j <= 0:
            tsec = float(tsel[0])
        elif j >= len(s_mon):
            continue
        else:
            s0, s1 = s_mon[j - 1], s_mon[j]
            frac = (s_stop[i] - s0) / max(1e-9, s1 - s0)
            tsec = float(tsel[j - 1] + frac * (tsel[j] - tsel[j - 1]))
        ev.append((ref_stops.stop_id.iloc[i], i + 1, float(s_stop[i]), tsec))
    n = 0
    for a in range(len(ev) - 1):
        dur = ev[a + 1][3] - ev[a][3]
        dist = ev[a + 1][2] - ev[a][2]
        if dur < MIN_EDGE_S or dur > MAX_EDGE_S or dist < -5:
            continue
        passages.append((trip_id, vehicle_id, route_id, ev[a][0], ev[a][1],
                         ev[a][3]))
        hour = int((ev[a][3] + 19800) // 3600 % 24)
        traversals.append((route_id, trip_id, ev[a][0], ev[a + 1][0],
                           round(dist, 1), round(dur, 1), int(ev[a][3]), hour))
        n += 1
    if ev:
        last = ev[-1]
        passages.append((trip_id, vehicle_id, route_id, last[0], last[1], last[3]))
        n += 1
    return n


def load_gtfs():
    shapes = ShapeCache().load()
    trips_df = pd.read_csv(GTFS / "trips.txt", dtype=str)
    times = pd.read_csv(GTFS / "stop_times.txt", dtype=str)
    stops = pd.read_csv(GTFS / "stops.txt")
    stops["stop_id"] = stops["stop_id"].astype(str)      # canonical string ids
    stops = stops.set_index("stop_id")
    stimes = times.copy()
    stimes["stop_sequence"] = stimes.stop_sequence.astype(int)
    stops_by_trip = {t: g.sort_values("stop_sequence")
                     for t, g in stimes.groupby("trip_id")}
    route_shapes = {}
    shape_ref = {}
    for r in trips_df.itertuples():
        route_shapes.setdefault(str(r.route_id), set()).add(str(r.shape_id))
        if str(r.shape_id) not in shape_ref:
            g = stops_by_trip.get(str(r.trip_id))
            if g is not None and len(g):
                shape_ref[str(r.shape_id)] = g
    return shapes, route_shapes, shape_ref, stops, trips_df


# ------------------------- worker process globals --------------------------
_W = {}


def _worker_init(mode):
    shapes, route_shapes, shape_ref, stops, trips_df = load_gtfs()
    trips_set = set(trips_df.trip_id.astype(str))
    trips_meta = trips_df.set_index("trip_id")
    _W.update(shapes=shapes, route_shapes=route_shapes, shape_ref=shape_ref,
              stops=stops, trips_set=trips_set, trips_meta=trips_meta, mode=mode)


def _trajectory(group):
    """Core per-trajectory processing. group = (trip_id, vehicle_id, proute,
    lat, lng, ts). Returns (passages, traversals, n_segments, matched)."""
    trip_id, vehicle_id, proute, plat, plng, pts = group
    passages, traversals = [], []
    if len(pts) == 0 or not np.isfinite(proute[0]):
        return passages, traversals, 0, 0
    rv = float(proute[0])
    route_id = str(int(rv)) if rv.is_integer() else str(rv)
    px = (plng - C_LNG) * K_LNG
    py = (plat - C_LAT) * K_LAT
    gaps = np.diff(pts)
    seg_starts = np.concatenate([[0], np.where(gaps > SEGMENT_GAP_S)[0] + 1])

    if _W["mode"] == "trips":
        cand_shapes = []
        meta = _W["trips_meta"].loc[trip_id] if trip_id in _W["trips_set"] else None
        if meta is not None:
            cand_shapes.append((route_id, str(meta.shape_id)))
    else:
        cand_shapes = [(route_id, sh)
                       for sh in sorted(_W["route_shapes"].get(route_id, []))]
    if not cand_shapes:
        return passages, traversals, 0, 0

    n_segs = matched = 0
    for si, start in enumerate(seg_starts):
        end = seg_starts[si + 1] if si + 1 < len(seg_starts) else len(pts)
        if end - start < MIN_TRIP_PINGS:
            continue
        n_segs += 1
        seg_px, seg_py, seg_t = px[start:end], py[start:end], pts[start:end]
        best = None
        for _, shape_id in cand_shapes:
            shape = _W["shapes"].get(shape_id)
            if shape is None:
                continue
            s, d = project_to_polyline(seg_px, seg_py, *shape)
            ok = d <= 2 * NEAR_SHAPE_M
            if ok.sum() < MIN_TRIP_PINGS:
                continue
            score = float(np.mean(np.sort(d)[:max(20, ok.sum() // 2)]))
            if best is None or score < best[0]:
                best = (score, shape_id, s, d)
        if best is None:
            continue
        matched += 1
        _, shape_id, s_all, d_all = best
        seg_mask = d_all <= NEAR_SHAPE_M
        ref = _W["shape_ref"].get(shape_id)
        passages_for_segment(s_all, d_all, seg_t, seg_mask,
                             _W["shapes"].get(shape_id), ref, _W["stops"],
                             trip_id, vehicle_id, route_id,
                             passages, traversals)
    return passages, traversals, n_segs, matched


def _worker_batch(batch):
    P, T, S, M = [], [], 0, 0
    for g in batch:
        p, t, s, m = _trajectory(g)
        P.extend(p); T.extend(t); S += s; M += m
    return P, T, S, M


# ---------------------------------------------------------------- streaming
def iter_groups(parquet_paths, mode, trips_set):
    """Yield materialized (trip_id, vehicle_id, route arr, lat, lng, ts)."""
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    files = ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in parquet_paths)
    q = f"""
        SELECT trip_id, vehicle_id, route_id, lat, lng, ts_utc
        FROM read_parquet([{files}]) ORDER BY trip_id, vehicle_id, ts_utc
    """
    cur = con.execute(q)

    def consume(rows, upto):
        if upto == 0:
            return
        head = rows[0]
        sel = rows[:upto]
        arr = np.array([(r[3], r[4], r[5], r[2] if r[2] is not None else np.nan)
                        for r in sel], dtype=float)
        yield (str(head[0]), str(head[1]),
               arr[:, 3], arr[:, 0], arr[:, 1], arr[:, 2])

    def split_point(rows):
        lo, hi = 0, len(rows)
        key = (rows[0][0], rows[0][1])
        while lo < hi:
            mid = (lo + hi) // 2
            if (rows[mid][0], rows[mid][1]) <= key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    batch_rows = []
    while True:
        rows = cur.fetchmany(100_000)
        if not rows:
            break
        batch_rows.extend(rows)
        while len(batch_rows) and \
                (batch_rows[0][0], batch_rows[0][1]) != (batch_rows[-1][0], batch_rows[-1][1]):
            cut = split_point(batch_rows)
            yield from consume(batch_rows, cut)
            batch_rows = batch_rows[cut:]
    if batch_rows:
        yield from consume(batch_rows, len(batch_rows))
    cur.close()
    con.close()


def detect_mode(day_pings_path, trips_set):
    con = duckdb.connect()
    sample = con.execute(f"""
        SELECT DISTINCT trip_id FROM read_parquet(
            '{str(day_pings_path).replace(chr(92), '/')}') USING SAMPLE 200 ROWS
    """).df()
    hits = sum(1 for t in sample.trip_id if str(t) in trips_set)
    con.close()
    return "trips" if hits >= len(sample) * 0.5 else "routes"


def main(day: str, mode: str = "auto", workers: int = 0):
    ensure_dirs()
    src = sorted(CLEAN.glob(f"pings_{day}.parquet"))
    if not src:
        raise SystemExit(f"[03] no pings_{day}.parquet — run 02 first")

    if mode == "auto":
        trips_quick = pd.read_csv(GTFS / "trips.txt", dtype=str)
        mode = detect_mode(src[0], set(trips_quick.trip_id.astype(str)))
    workers = workers or max(1, (os.cpu_count() or 4) // 2)
    print(f"[03] {day}: mode={mode} workers={workers}")

    passages, traversals = [], []
    n_trips = n_segs_total = n_matched = 0

    def absorb(result):
        nonlocal n_segs_total, n_matched
        P, T, S, M = result
        passages.extend(P)
        traversals.extend(T)
        n_segs_total += S
        n_matched += M

    if workers <= 1:
        _worker_init(mode)
        for g in iter_groups(src, mode, _W.get("trips_set", set())):
            n_trips += 1
            absorb(_trajectory(g))
    else:
        with mp.Pool(workers, initializer=_worker_init, initargs=(mode,)) as pool:
            batch, count = [], 0
            for g in iter_groups(src, mode, set()):
                n_trips += 1
                batch.append(g)
                if len(batch) >= BATCH_TRAJECTORIES:
                    absorb(pool.apply(_worker_batch, (batch,)))
                    batch = []
                    if n_trips % 5000 < BATCH_TRAJECTORIES:
                        print(f"[03] {day}: {n_trips:,} trajectories in ...",
                              flush=True)
            if batch:
                absorb(pool.apply(_worker_batch, (batch,)))

    pd.DataFrame(passages, columns=["trip_id", "vehicle_id", "route_id", "stop_id",
                                    "stop_seq", "tsec"]) \
      .to_parquet(CLEAN / f"passages_{day}.parquet", index=False)
    pd.DataFrame(traversals, columns=["route_id", "trip_id", "from_stop", "to_stop",
                                      "dist_m", "dur_s", "tsec", "hour_ist"]) \
      .to_parquet(CLEAN / f"traversals_{day}.parquet", index=False)
    print(f"[03] {day}: {n_trips:,} trips scanned, {n_segs_total:,} segments, "
          f"{n_matched:,} matched -> {len(passages):,} passages, "
          f"{len(traversals):,} traversals")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026_10_15")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mode", default="auto", choices=["auto", "trips", "routes"])
    ap.add_argument("--workers", type=int, default=0,
                    help="0 = auto (cpu/2); 1 = single process")
    a = ap.parse_args()
    days = [a.day]
    if a.all:
        days = [p.stem.replace("pings_", "")
                for p in sorted(CLEAN.glob("pings_*.parquet"))]
    for d in days:
        main(d, a.mode, a.workers)
