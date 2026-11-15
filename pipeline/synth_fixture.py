"""
Dress rehearsal: fabricate a miniature Delhi bus network + one day of GPS in the
EXACT schema of the real dataset, so the whole pipeline can be built and tested
before the 16 GB download finishes.

Outputs:
  data/gtfs/*.txt                 (stops, routes, trips, stop_times, shapes, ...)
  data/raw/bus_movements_2026_10_15.db   (vehicle_feed table, real schema)
"""
import sqlite3
import math
import random
import numpy as np
import pandas as pd

from common import ensure_dirs, RAW, GTFS, IST_OFFSET_S

random.seed(42)
np.random.seed(42)

DAY = "2026_10_15"
DATE_STR = "2026-10-15"

# Center the fake network around central Delhi
C_LAT, C_LNG = 28.62, 77.21
M_LAT, M_LNG = 110540.0, 111320.0  # meters per degree


def xy2ll(x, y):
    return (y / M_LAT + C_LAT, x / (M_LNG * math.cos(math.radians(C_LAT))) + C_LNG)


def ll2xy(lat, lng):
    return ((lng - C_LNG) * M_LNG * math.cos(math.radians(C_LAT)), (lat - C_LAT) * M_LAT)


ROUTES = [
    ("543", "CL-Green",      [(0, 0), (6000, 2500), (9000, 9000)]),
    ("419", "Ring Express",  [(-4000, -3000), (2000, 1000), (8000, -2000)]),
    ("744", "N-S trunk",     [(-2000, 9000), (500, 2000), (3000, -7000)]),
]


def build_route_geometry(knees, n_stops=10, n_shape_pts=50):
    """Linear interpolation through knees with a slight sine bend."""
    t = np.linspace(0, 1, n_shape_pts)
    sx = np.interp(t, np.linspace(0, 1, len(knees)), [k[0] for k in knees])
    sy = np.interp(t, np.linspace(0, 1, len(knees)), [k[1] for k in knees])
    seg = np.linspace(0, np.pi, n_shape_pts)
    sx = sx + 250 * np.sin(seg * 2)
    sy = sy + 250 * np.cos(seg * 3)
    # stops evenly along the shape by index
    stop_idx = np.linspace(0, n_shape_pts - 1, n_stops).round().astype(int)
    return sx, sy, stop_idx


def shape_dists(sx, sy):
    d = [0.0]
    for i in range(1, len(sx)):
        d.append(d[-1] + math.hypot(sx[i] - sx[i - 1], sy[i] - sy[i - 1]))
    return np.array(d)


def hhmmss(sec):
    h = sec // 3600; m = (sec % 3600) // 60; s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    ensure_dirs()
    stops_rows, shapes_rows, trips_rows, stop_times_rows, routes_rows = [], [], [], [], []
    routes_rows.append(dict(agency_id="DTC", route_id="543", route_short_name="543",
                            route_long_name="CL-Green", route_type=3))
    routes_rows.append(dict(agency_id="DTC", route_id="419", route_short_name="419EX",
                            route_long_name="Ring Express", route_type=3))
    routes_rows.append(dict(agency_id="DTsC", route_id="744", route_short_name="744",
                            route_long_name="N-S trunk", route_type=3))
    # fix typo agency above
    routes_rows[2]["agency_id"] = "DTC"
    agency_row = "agency_id,agency_name,agency_url,agency_timezone,agency_lang\nDTC,Delhi Transport Corp,https://dtc.delhi.gov.in,Asia/Kolkata,en\n"

    stop_counter = 0
    trip_counter = 0
    ping_rows = []   # (vehicle_id, lat, lng, speed, route_id, trip_id, ts_utc, date, time)

    for route_id, long_name, knees in ROUTES:
        for direction in (0, 1):
            sx, sy, stop_idx = build_route_geometry(knees)
            if direction == 1:
                sx, sy = sx[::-1], sy[::-1]
                stop_idx = (len(sx) - 1) - stop_idx[::-1]
            dist = shape_dists(sx, sy)
            shape_id = f"shp_{route_id}_{direction}"

            for i, (x, y) in enumerate(zip(sx, sy)):
                lat, lng = xy2ll(x, y)
                shapes_rows.append(dict(shape_id=shape_id, shape_pt_lat=lat,
                                         shape_pt_lon=lng, shape_pt_sequence=i + 1))
            pattern_stops = []
            for seq, si in enumerate(stop_idx):
                stop_counter += 1
                sid = f"S{stop_counter:04d}"
                lat, lng = xy2ll(sx[si], sy[si])
                stops_rows.append(dict(stop_id=sid, stop_code="", stop_name=f"Stop {route_id}{'U' if direction==0 else 'D'} {seq}",
                                       stop_desc="", stop_lat=lat, stop_lon=lng, location_type=0))
                pattern_stops.append((sid, dist[si]))

            # trips through the day, hourly-ish headways with rush-hour extras
            departures = list(range(6 * 3600, 22 * 3600, 3600))
            departures += [8 * 3600 + 1800, 10 * 3600 + 1800, 18 * 3600 + 1800]
            departures.sort()

            for dep_sec_ist in departures:
                trip_counter += 1
                trip_id = f"TRIP_{trip_counter:05d}"
                trips_rows.append(dict(route_id=route_id, service_id="ALL", trip_id=trip_id,
                                       shape_id=shape_id, direction_id=direction))
                for seq, (sid, s_along) in enumerate(pattern_stops):
                    # synthetic constant-speed schedule (like real GTFS: rough)
                    sched = dep_sec_ist + s_along / 9.0  # ~9 m/s idle speed
                    stop_times_rows.append(dict(trip_id=trip_id, arrival_time=hhmmss(sched),
                                                 departure_time=hhmmss(sched), stop_id=sid,
                                                 stop_sequence=seq + 1,
                                                 shape_dist_traveled=round(s_along, 1)))

                # ---- GPS trace for this trip ----
                total_m = dist[-1]
                # speed profile: rushes slow (x0.45), midday ok
                h_ist = dep_sec_ist // 3600
                mult = 0.45 if (7 <= h_ist <= 10 or 17 <= h_ist <= 20) else 1.0
                v_base = 11.0 * mult * random.uniform(0.85, 1.15)  # m/s
                veh = f"DL1P{random.randint(1000, 9999)}"
                t = 0.0
                s = 0.0
                # dwell at each stop (deterministic-ish)
                stop_s_set = {d_ for _, d_ in pattern_stops}
                while s < total_m:
                    v = v_base * (0.75 if (s % 1200) < 400 else 1.0)  # pseudo signal-jerk profile
                    dt = 10.0
                    s_next = min(total_m, s + v * dt)
                    # if crossing a stop, add dwell by advancing time
                    crossed = [d_ for d_ in stop_s_set if s < d_ <= s_next]
                    # advance along shape with linear interpolation
                    for frac in np.linspace(0, 1, 5, endpoint=True):
                        s_pos = s + (s_next - s) * frac
                        # find xy and time at s_pos
                        ix = int(np.searchsorted(dist, s_pos)) - 1
                        ix = max(0, min(len(dist) - 2, ix))
                        seg_frac = (s_pos - dist[ix]) / max(1e-9, dist[ix + 1] - dist[ix])
                        x = sx[ix] + (sx[ix + 1] - sx[ix]) * seg_frac
                        y = sy[ix] + (sy[ix + 1] - sy[ix]) * seg_frac
                        ts = t + (s_next - s) / max(0.1, v) * frac
                        jx = x + random.gauss(0, 12); jy = y + random.gauss(0, 12)
                        lat, lng = xy2ll(jx, jy)
                        speed_kmh = v * 3.6
                        ts_utc = int((dep_sec_ist + ts) - IST_OFFSET_S)
                        rec = (veh, f"{lat:.6f}", f"{lng:.6f}", speed_kmh,
                               float(route_id) if route_id.isdigit() else None,
                               trip_id, str(ts_utc), DATE_STR,
                               hhmmss(int(dep_sec_ist + ts)))
                        ping_rows.append(rec)
                        if random.random() < 0.004:   # injection: far-off GPS outlier
                            blat, blng = xy2ll(x + 800, y + 800)
                            ping_rows.append((veh, f"{blat:.6f}", f"{blng:.6f}", 0.0,
                                              float(route_id), trip_id, str(ts_utc + 7),
                                              DATE_STR, hhmmss(int(dep_sec_ist + ts))))
                        if random.random() < 0.003:   # injection: junk text row
                            ping_rows.append((veh, "abc", f"{lng:.6f}", 0.0, float(route_id),
                                              trip_id, str(ts_utc), DATE_STR, "xx:xx:xx"))
                    s = s_next
                    t += dt + (30.0 if crossed else 0.0)  # dwell approx

    # ---- write GTFS ----
    pd.DataFrame(stops_rows).to_csv(GTFS / "stops.txt", index=False)
    pd.DataFrame(shapes_rows).to_csv(GTFS / "shapes.txt", index=False)
    pd.DataFrame(trips_rows).to_csv(GTFS / "trips.txt", index=False)
    pd.DataFrame(stop_times_rows).to_csv(GTFS / "stop_times.txt", index=False)
    pd.DataFrame(routes_rows).to_csv(GTFS / "routes.txt", index=False)
    (GTFS / "agency.txt").write_text(agency_row, encoding="utf-8")
    (GTFS / "calendar.txt").write_text(
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "ALL,1,1,1,1,1,1,1,20260101,20261231\n", encoding="utf-8")

    # ---- write one day of GPS in the REAL schema ----
    db = RAW / f"bus_movements_{DAY}.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE vehicle_feed (
        vehicle_id TEXT, lat TEXT, lng TEXT, speed REAL, route_id REAL,
        trip_id TEXT, timestamp TEXT, date DATE, time TEXT)""")
    con.executemany("INSERT INTO vehicle_feed VALUES (?,?,?,?,?,?,?,?,?)", ping_rows)
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM vehicle_feed").fetchone()[0]
    n_tr = len(set(r[5] for r in ping_rows))
    con.close()
    print(f"synth GTFS written: {len(stops_rows)} stops, {len(trips_rows)} trips, {len(shapes_rows)} shape pts")
    print(f"synth GPS day {DAY}: {n} pings across {n_tr} trips -> {db}")


if __name__ == "__main__":
    main()
