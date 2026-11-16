"""Export model tables -> D1-importable SQL, sized for the D1 free plan.

Free-plan constraint: 100,000 ROWS WRITTEN per day. Affected rows count,
so `DELETE` + `INSERT OR REPLACE` double-charges. Strategy here:
  * DROP TABLE + CREATE TABLE (DDL writes are not row writes)
  * plain INSERT only  (no REPLACE -> no extra write per row)
  * evidence-ranked row budget (default 80,000 total) so the full-month
    model can never push an import over the daily cap
Tables: city_hour_real, route_hour_real, route_speed_real, geo_shapes,
wait_headway, meta. Output: docs/d1_export.sql (sharded by split_sql).
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import MODEL, GTFS  # noqa: E402

ROW_BUDGET = int(__import__("os").environ.get("DP_D1_ROW_BUDGET", "80000"))


def esc(v):
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    f = float(v)
    return str(int(f)) if f.is_integer() else str(round(f, 4))


def emit_table(out, name, schema, cols, rows, batch=250):
    """DROP + CREATE + batched plain INSERTs. Returns rows written."""
    out.append(f"DROP TABLE IF EXISTS {name};")
    out.append(f"CREATE TABLE {name} {schema};")
    written = 0
    for i in range(0, len(rows), batch):
        vals = ", ".join(
            "(" + ", ".join(esc(v) for v in row) + ")" for row in rows[i:i + batch])
        out.append(f"INSERT INTO {name} ({', '.join(cols)}) VALUES {vals};")
        written += len(rows[i:i + batch])
    return written


def main():
    total_budget = ROW_BUDGET

    out = ["PRAGMA foreign_keys=OFF;"]
    written = 0

    # ---- city_hour_real (24 rows) ----
    ch = pd.read_parquet(MODEL / "city_hour_real.parquet")
    written += emit_table(out, "city_hour_real",
        "(hour_ist INTEGER PRIMARY KEY, speed_kmh REAL, n INTEGER)",
        ["hour_ist", "speed_kmh", "n"],
        [(int(r.hour_ist), float(r.speed_kmh), int(r.n)) for r in ch.itertuples()])
    print(f"city_hour_real: 24 rows")

    # ---- route_hour_real (evidence-ranked to budget) ----
    rh = pd.read_parquet(MODEL / "route_hour_real.parquet")
    rh = rh[rh.n >= 3]                                    # drop noise cells
    rh = rh.sort_values("n", ascending=False)
    take = max(1000, total_budget // 2 - 2000)            # ~half the budget
    rh = rh.head(take)
    written += emit_table(out, "route_hour_real",
        "(route_id TEXT, hour_ist INTEGER, speed_kmh REAL, n INTEGER,"
        " PRIMARY KEY(route_id, hour_ist))",
        ["route_id", "hour_ist", "speed_kmh", "n"],
        [(str(r.route_id), int(r.hour_ist), float(r.speed_kmh), int(r.n))
         for r in rh.itertuples()], batch=250)
    print(f"route_hour_real: {len(rh)} rows (budget-capped, n>=3)")

    # ---- route_speed_real ----
    rs = pd.read_parquet(MODEL / "route_speed_real.parquet")
    written += emit_table(out, "route_speed_real",
        "(route_id TEXT PRIMARY KEY, speed_kmh REAL, days INTEGER)",
        ["route_id", "speed_kmh", "days"],
        [(str(r.route_id), float(r.speed_kmh), int(r.days))
         for r in rs.itertuples()])
    print(f"route_speed_real: {len(rs)} rows")

    # ---- geo_shapes (one row per route, thinned coords) ----
    import json as _json
    shapes = {}
    for sid, g in pd.read_csv(GTFS / "shapes.txt").groupby("shape_id", sort=False):
        g = g.sort_values("shape_pt_sequence")
        shapes[str(sid)] = list(zip(g.shape_pt_lat.astype(float),
                                    g.shape_pt_lon.astype(float)))
    rshapes = {}
    for r in pd.read_csv(GTFS / "trips.txt", dtype=str).itertuples():
        rshapes.setdefault(str(r.route_id), str(r.shape_id))
    names = pd.read_csv(GTFS / "routes.txt", dtype=str)
    names["route_id"] = names.route_id.astype(str)
    nmap = dict(zip(names.route_id,
                    names.route_short_name.fillna(names.route_long_name)))
    rows_geo = []
    for rid, sid in rshapes.items():
        pts = shapes.get(sid)
        if not pts:
            continue
        stride = max(1, len(pts) // 120)
        coords = [[round(lng, 5), round(lat, 5)] for lat, lng in pts[::stride]]
        if len(coords) < 2:
            continue
        rows_geo.append((rid, str(nmap.get(rid, rid))[:60],
                         _json.dumps(coords)))
    # keep only routes that survived the route_hour budget + a fixed slice
    keep = set(rh.route_id.astype(str))
    rows_geo = [r for r in rows_geo if r[0] in keep][: (total_budget // 8)]
    written += emit_table(out, "geo_shapes",
        "(route_id TEXT PRIMARY KEY, name TEXT, coords TEXT)",
        ["route_id", "name", "coords"], rows_geo, batch=8)
    print(f"geo_shapes: {len(rows_geo)} routes")

    # ---- wait_headway (evidence-ranked to remaining budget) ----
    wh_path = MODEL / "wait_headway_real.parquet"
    written_wh = 0
    if wh_path.exists():
        wh = pd.read_parquet(wh_path)
        remaining = max(2000, total_budget - written - 3000)
        wh = wh.sort_values("n_services_per_day", ascending=False).head(remaining)
        written_wh = emit_table(out, "wait_headway",
            "(route_id TEXT, hour_ist INTEGER, headway_s REAL,"
            " services_per_day REAL, PRIMARY KEY(route_id, hour_ist))",
            ["route_id", "hour_ist", "headway_s", "services_per_day"],
            [(str(r.route_id), int(r.hour_ist), float(r.headway_s),
              float(r.n_services_per_day)) for r in wh.itertuples()],
            batch=150)
        print(f"wait_headway: {len(wh)} route-hour cells (budget-capped)")
    written += written_wh

    # ---- meta ----
    meta = {
        "version": "v2-full-month",
        "exported_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "row_budget": str(total_budget),
        "rows_written": str(written),
    }
    out.append("DROP TABLE IF EXISTS meta;")
    out.append("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);")
    for k, v in meta.items():
        out.append(f"INSERT INTO meta VALUES ('{k}', '{v}');")
    written += len(meta)

    dst = Path(__file__).resolve().parent.parent / "docs" / "d1_export.sql"
    dst.write_text("\n".join(out))
    print(f"TOTAL ROWS WRITTEN: {written} (budget {total_budget}) "
          f"{'OK' if written < 100000 else 'OVER FREE LIMIT!'}")
    print(f"sql -> {dst} ({dst.stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
