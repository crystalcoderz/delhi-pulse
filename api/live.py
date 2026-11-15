"""
Live GTFS-RT layer: poll VehiclePositions, keep a rolling store of recent
positions, compute per-route window speeds for the blend.

The SAME window-speed computation powers replay mode (against the October
archive -> pipeline windows) — one blend path, two sources, as designed.
"""
import collections
import math
import threading
import time

import httpx
from google.transit import gtfs_realtime_pb2

C_LAT, C_LNG = 28.62, 77.21
K_LAT, K_LNG = 110540.0, 111320.0 * math.cos(math.radians(C_LAT))


def dist_m(lat0, lng0, lat1, lng1):
    return math.hypot((lng1 - lng0) * K_LNG, (lat1 - lat0) * K_LAT)


class LiveLayer:
    def __init__(self, url=None, poll_s=10.0, keep_s=2700.0, headers=None):
        self.url = url
        self.poll_s = poll_s
        self.keep_s = keep_s
        self.headers = headers or {}          # e.g. {"x-api-key": ...}
        self.store = collections.defaultdict(collections.deque)
        # vehicle_id -> deque of (ts, lat, lng, route_id)
        self.lock = threading.Lock()
        self.last_fetch_ts = None
        self.last_error = None
        self.n_fetch = 0
        self._t = None

    # ---------------- polling ----------------
    def start(self):
        if self._t is not None or not self.url:
            return False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return True

    def _loop(self):
        while True:
            try:
                self.fetch_once()
            except Exception as e:  # keep the loop alive
                self.last_error = f"{type(e).__name__}: {e}"
            time.sleep(self.poll_s)

    def fetch_once(self):
        r = httpx.get(self.url, timeout=20.0, headers=self.headers)
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(r.content)
        added = 0
        with self.lock:
            for ent in feed.entity:
                vp = ent.vehicle
                if not vp.HasField("position") or not vp.HasField("timestamp"):
                    continue
                route = ""
                if vp.HasField("trip") and vp.trip.route_id:
                    route = str(vp.trip.route_id)
                veh = vp.vehicle.id or ent.id or f"v{len(self.store)}"
                dq = self.store[veh]
                dq.append((vp.timestamp, vp.position.latitude,
                           vp.position.longitude, route))
                added += 1
            cutoff = time.time() - self.keep_s
            for v in list(self.store):
                dq = self.store[v]
                while dq and dq[0][0] < cutoff:
                    dq.popleft()
                if not dq:
                    del self.store[v]
        self.n_fetch += 1
        self.last_fetch_ts = time.time()
        return added

    # ---------------- blend inputs ----------------
    def window_speeds(self, window_s=900.0, min_pairs=3):
        """Per-route implied mean speed over the recent window.
        Returns {route_id: (speed_kmh, n_pairs)}."""
        cutoff = time.time() - window_s
        agg = {}
        with self.lock:
            for v, dq in self.store.items():
                pts = [p for p in dq if p[0] >= cutoff]
                for (t0, la0, lo0, r0), (t1, la1, lo1, r1) in zip(pts, pts[1:]):
                    dt = t1 - t0
                    if dt <= 0 or dt > 120 or not r0 or r0 != r1:
                        continue
                    d = dist_m(la0, lo0, la1, lo1)
                    if d < 15 or d / dt * 3.6 > 90:
                        continue
                    cell = agg.setdefault(r0, [0.0, 0.0, 0])
                    cell[0] += d
                    cell[1] += dt
                    cell[2] += 1
        return {r: (c[0] / c[1] * 3.6, c[2]) for r, c in agg.items()
                if c[2] >= min_pairs}

    def status(self):
        with self.lock:
            return dict(url=bool(self.url), vehicles=len(self.store),
                        n_fetch=self.n_fetch,
                        last_fetch=self.last_fetch_ts, last_error=self.last_error)


LIVE = LiveLayer()  # module singleton; url injected from env at app start
