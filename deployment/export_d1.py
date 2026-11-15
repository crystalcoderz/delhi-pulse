"""Export real model tables -> D1-importable SQL (docs/d1_export.sql).
Tables: city_hour_real, route_hour_real, route_speed_real, meta.
Sizes are small (tens of thousands of rows) - one-shot import is fine.
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import MODEL  # noqa: E402


def esc(v):
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    f = float(v)
    return str(int(f)) if f.is_integer() else str(round(f, 4))


def main():
    out = ["PRAGMA foreign_keys=OFF;",
           "DELETE FROM city_hour_real;",
           "DELETE FROM route_hour_real;",
           "DELETE FROM route_speed_real;"]
    tables = [
        ("city_hour_real", MODEL / "city_hour_real.parquet"),
        ("route_hour_real", MODEL / "route_hour_real.parquet"),
        ("route_speed_real", MODEL / "route_speed_real.parquet"),
    ]
    schema = {
        "city_hour_real": "(hour_ist INTEGER PRIMARY KEY, speed_kmh REAL, n INTEGER)",
        "route_hour_real": "(route_id TEXT, hour_ist INTEGER, speed_kmh REAL, n INTEGER, PRIMARY KEY(route_id, hour_ist))",
        "route_speed_real": "(route_id TEXT PRIMARY KEY, speed_kmh REAL, days INTEGER)",
    }
    for name, path in tables:
        if not path.exists():
            print(f"skip {name}: {path} missing")
            continue
        df = pd.read_parquet(path)
        out.append(f"CREATE TABLE IF NOT EXISTS {name} {schema[name]};")
        cols = {  # normalize to expected cols
            "city_hour_real": ["hour_ist", "speed_kmh", "n"],
            "route_hour_real": ["route_id", "hour_ist", "speed_kmh", "n"],
            "route_speed_real": ["route_id", "speed_kmh", "days"],
        }[name]
        rows = []
        for r in df.itertuples(index=False):
            d = dict(zip(df.columns, r))
            rows.append(tuple(d.get(c) for c in cols))
        B = 400
        for i in range(0, len(rows), B):
            values = ", ".join(
                "(" + ", ".join(esc(v) for v in row) + ")" for row in rows[i:i + B])
            out.append(
                f"INSERT OR REPLACE INTO {name} ({', '.join(cols)}) VALUES {values};")
        print(f"{name}: {len(rows)} rows")
    out.append("CREATE TABLE IF NOT EXISTS meta "
               "(k TEXT PRIMARY KEY, v TEXT);")
    out.append("DELETE FROM meta;")
    meta = {
        "version": "v1-pings-real",
        "exported_utc": dt.datetime.utcnow().isoformat(),
    }
    for k, v in meta.items():
        out.append(f"INSERT OR REPLACE INTO meta VALUES ('{k}', '{v}');")

    # ---- v2 tables: shapes + headways for the edge city-pulse ----
    import json as _json
    out.append("CREATE TABLE IF NOT EXISTS geo_shapes ("
               "route_id TEXT PRIMARY KEY, name TEXT, coords TEXT);")
    out.append("DELETE FROM geo_shapes;")
    try:
        glo = sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
        from common import GTFS as _G
        _shapes = {}
        for sid, g in pd.read_csv(_G / "shapes.txt").groupby("shape_id", sort=False):
            g = g.sort_values("shape_pt_sequence")
            _shapes[str(sid)] = list(zip(g.shape_pt_lat.astype(float),
                                          g.shape_pt_lon.astype(float)))
        _rshapes = {}
        for r in pd.read_csv(_G / "trips.txt", dtype=str).itertuples():
            _rshapes.setdefault(str(r.route_id), str(r.shape_id))
        _names = pd.read_csv(_G / "routes.txt", dtype=str)
        _names["route_id"] = _names.route_id.astype(str)
        _nmap = dict(zip(_names.route_id, _names.route_short_name.fillna(_names.route_long_name)))
        rows_geo = []
        for rid, sid in _rshapes.items():
            pts = _shapes.get(sid)
            if not pts:
                continue
            stride = max(1, len(pts) // 120)
            coords = [[round(lng, 5), round(lat, 5)]
                      for lat, lng in pts[::stride]]
            if len(coords) < 2:
                continue
            rows_geo.append((rid, str(_nmap.get(rid, rid))[:60],
                             _json.dumps(coords)))
        B = 50
        for i in range(0, len(rows_geo), B):
            vals = ", ".join(
                f"('{r}', '{n}', '{c}')" for r, n, c in rows_geo[i:i + B])
            out.append(f"INSERT OR REPLACE INTO geo_shapes VALUES {vals};")
        print(f"geo_shapes: {len(rows_geo)} routes -> "
              f"{sum(len(c) for r, n, c in rows_geo)/1e6:.1f} MB coord JSON")
    except Exception as e:
        print(f"geo_shapes export skipped: {e}")

    if (MODEL / "wait_headway_real.parquet").exists():
        wh = pd.read_parquet(MODEL / "wait_headway_real.parquet")
        out.append("CREATE TABLE IF NOT EXISTS wait_headway ("
                   "route_id TEXT, hour_ist INTEGER, headway_s REAL, "
                   "services_per_day REAL, PRIMARY KEY(route_id, hour_ist));")
        out.append("DELETE FROM wait_headway;")
        rows_w = [(str(r.route_id), int(r.hour_ist), float(r.headway_s),
                   float(r.n_services_per_day)) for r in wh.itertuples()]
        B = 400
        for i in range(0, len(rows_w), B):
            vals = ", ".join(f"('{r}',{h},{round(s,1)},{round(n,2)})"
                             for r, h, s, n in rows_w[i:i + B])
            out.append(f"INSERT OR REPLACE INTO wait_headway VALUES {vals};")
        print(f"wait_headway: {len(rows_w)} route-hour cells")
    dst = Path(__file__).resolve().parent.parent / "docs" / "d1_export.sql"
    dst.write_text("\n".join(out))
    print(f"sql -> {dst} ({dst.stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
