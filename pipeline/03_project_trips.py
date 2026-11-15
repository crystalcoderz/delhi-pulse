"""
Stage 3: project clean pings onto their trip's GTFS shape -> stop passage events
-> edge traversal samples.

This is the core algorithm. For every (trip, vehicle) trajectory we:
  1. load the trip's shape polyline (from GTFS), precompute cumulative arc length
  2. project each ping onto the polyline (nearest segment, numpy-vectorized)
  3. drop pings >NEAR_SHAPE_M away (bus off-route / wrong trip_id mapping)
  4. enforce monotone progress (running max)
  5. project the trip's stops onto the same polyline
  6. interpolate crossing time for each stop -> passage events
  7. consecutive passages -> edge traversal samples

Outputs per day:
  data/clean/passages_YYYY_MM_DD.parquet
  data/clean/traversals_YYYY_MM_DD.parquet
Usage:
  python pipeline/03_project_trips.py --day 2026_10_15
"""
import argparse
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from common import (ensure_dirs, CLEAN, GTFS,
                    NEAR_SHAPE_M, NEAR_STOP_M, MIN_TRIP_PINGS,
                    MIN_EDGE_S, MAX_EDGE_S)

C_LAT, C_LNG = 28.62, 77.21
K_LAT, K_LNG = 110540.0, 111320.0 * math.cos(math.radians(C_LAT))


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
            self.shapes[sid] = (x, y, cum)
        return self

    def get(self, shape_id):
        return self.shapes.get(shape_id)


def project_to_polyline(px, py, x, y, cum, chunk=256):
    """Project points onto polyline. Returns (arclength s, distance d) per point."""
    ax, ay = x[:-1], y[:-1]
    bx, by = x[1:], y[1:]
    vx, vy = bx - ax, by - ay
    seg_len2 = vx * vx + vy * vy
    seg_len2 = np.where(seg_len2 < 1e-9, 1e-9, seg_len2)
    s_out = np.empty(len(px))
    d_out = np.empty(len(px))
    for i in range(0, len(px), chunk):
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


def iter_trips(parquet_paths):
    """Stream pings grouped by (trip_id, vehicle_id) in memory-bounded batches."""
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    files = ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in parquet_paths)
    q = f"""
        SELECT trip_id, vehicle_id, route_id, lat, lng, speed, ts_utc,
               hour_ist, dow_ist, dt_ist
        FROM read_parquet([{files}]) ORDER BY trip_id, vehicle_id, ts_utc
    """
    cur = con.execute(q)
    batch = []
    cur_key, prev_key = None, None
    while True:
        rows = cur.fetchmany(100_000)
        if not rows:
            break
        batch.extend(rows)
        while batch:
            key = (batch[0][0], batch[0][1])
            last = len(batch) - 1
            next_key = (batch[last][0], batch[last][1])
            if next_key == key:
                break
            # find boundary where key changes
            # (rows are sorted; do a linear scan from the end is O(n^2) worst case,
            #  so binary search instead)
            lo = batch_searchsplit(batch, key)
            yield from consume(batch, lo)
            batch = batch[lo:]
    cur.close()
    con.close()
    if batch:
        yield from consume(batch, len(batch))


def batch_searchsplit(rows, key):
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if (rows[mid][0], rows[mid][1]) <= key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def consume(rows, upto):
    """Yield (trip_id, vehicle_id, np arrays) for the contiguous group at the head."""
    if upto == 0:
        return
    head = rows[0]
    key = (head[0], head[1])
    sel = rows[:upto]
    arr = np.array([(r[3], r[4], r[6]) for r in sel], dtype=float)  # lat, lng, ts
    yield (key, head[2], arr[:, 0], arr[:, 1], arr[:, 2])


def main(day: str):
    ensure_dirs()
    src = sorted(CLEAN.glob(f"pings_{day}.parquet"))
    if not src:
        raise SystemExit(f"[03] no pings_{day}.parquet — run 02 first")

    shapes = ShapeCache().load()
    tripmeta = pd.read_csv(GTFS / "trips.txt").set_index("trip_id")
    times = pd.read_csv(GTFS / "stop_times.txt")
    stop_xy = pd.read_csv(GTFS / "stops.txt").set_index("stop_id")
    stops_by_trip = {t: g.sort_values("stop_sequence") for t, g in times.groupby("trip_id")}
    dist_by_trip = {t: dict(zip(g.stop_id, g.shape_dist_traveled)) for t, g in times.groupby("trip_id")}

    passages, traversals = [], []
    n_trips = n_kept = 0

    for (trip_id, vehicle_id), route_id, plat, plng, pts in iter_trips(src):
        n_trips += 1
        if len(pts) < MIN_TRIP_PINGS or trip_id not in tripmeta.index:
            continue
        meta = tripmeta.loc[trip_id]
        shape = shapes.get(meta.shape_id)
        if shape is None:
            continue
        x, y, cum = shape
        px = (plng - C_LNG) * K_LNG
        py = (plat - C_LAT) * K_LAT
        s, d = project_to_polyline(px, py, x, y, cum)
        ok = d <= NEAR_SHAPE_M
        if ok.sum() < MIN_TRIP_PINGS:
            continue
        s = np.maximum.accumulate(s[ok])          # monotone progress
        t_ = pts[ok]
        n_kept += 1

        st = stops_by_trip.get(trip_id)
        if st is None or len(st) == 0:
            continue
        sx = (stop_xy.loc[st.stop_id, "stop_lon"].to_numpy() - C_LNG) * K_LNG
        sy = (stop_xy.loc[st.stop_id, "stop_lat"].to_numpy() - C_LAT) * K_LAT
        ss, sd = project_to_polyline(sx, sy, x, y, cum)
        good = sd <= NEAR_STOP_M
        if good.sum() < 2:
            continue

        ev = []
        prev_s = -1.0
        for i in range(len(st)):
            if not good[i] or ss[i] <= prev_s:
                continue
            prev_s = ss[i]
            j = np.searchsorted(s, ss[i])
            if j <= 0:
                tsec = float(t_[0])
            elif j >= len(s):
                continue
            else:
                s0, s1 = s[j - 1], s[j]
                frac = (ss[i] - s0) / max(1e-9, s1 - s0)
                tsec = float(t_[j - 1] + frac * (t_[j] - t_[j - 1]))
            sid = st.stop_id.iloc[i]
            ev.append((sid, i + 1, float(ss[i]), tsec))

        for a in range(len(ev) - 1):
            dur = ev[a + 1][3] - ev[a][3]
            dist = ev[a + 1][2] - ev[a][2]
            if dur < MIN_EDGE_S or dur > MAX_EDGE_S or dist < -5:
                continue
            hour = int((ev[a][3] + 19800) // 3600 % 24)
            passages.append((trip_id, vehicle_id, route_id, ev[a][0], ev[a][1], tsec))
            traversals.append((route_id, trip_id, ev[a][0], ev[a + 1][0],
                               round(dist, 1), round(dur, 1), int(ev[a][3]),
                               hour))
        if ev:
            last = ev[-1]
            passages.append((trip_id, vehicle_id, route_id, last[0], last[1], last[3]))

    pd.DataFrame(passages, columns=["trip_id", "vehicle_id", "route_id", "stop_id",
                                    "stop_seq", "tsec"]) \
      .to_parquet(CLEAN / f"passages_{day}.parquet", index=False)
    pd.DataFrame(traversals, columns=["route_id", "trip_id", "from_stop", "to_stop",
                                      "dist_m", "dur_s", "tsec", "hour_ist"]) \
      .to_parquet(CLEAN / f"traversals_{day}.parquet", index=False)
    print(f"[03] {day}: {n_trips:,} trips scanned, {n_kept:,} usable -> "
          f"{len(passages):,} passages, {len(traversals):,} traversals")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026_10_15")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    days = [a.day]
    if a.all:
        days = [p.stem.replace("pings_", "")
                for p in sorted(CLEAN.glob("pings_*.parquet"))]
    for d in days:
        main(d)
