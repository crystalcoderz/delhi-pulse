"""Export model tables -> D1-importable SQL, sized for the D1 free plan.

Free-plan constraint: 100,000 ROWS WRITTEN per day. Affected rows count,
so `DELETE` + `INSERT OR REPLACE` double-charges. Strategy here:
  * DROP TABLE + CREATE TABLE (DDL writes are not row writes)
  * plain INSERT only  (no REPLACE -> no extra write per row)
  * evidence-ranked row budget (default 80,000 total) so the full-month
    model can never push an import over the daily cap
Tables: city_hour_real, route_hour_real, route_speed_real, geo_shapes,
wait_headway, meta. Output: docs/d1_export.sql, then split_sql shards it
into docs/d1_partN.sql IN THIS SAME PROCESS (fix-plan F2: export+split are
atomic — the split asserts the exported_utc stamp this run just wrote, so
a stale d1_export.sql can never be sharded by accident).

Fix-plan F:
  * Speed tables prefer the TRAVERSAL-trained parquets (model/route_hour
    .parquet ...) and fall back to the ping-pair *_real.parquet only when
    the traversal file is missing or empty — same policy as
    export_model_json (B1). Every data row carries a `source` column
    ('traversal'|'ping-pair') and meta carries a `family` row, so the edge
    can tell which corpus it holds.
  * esc() maps None/NaN/±Inf -> NULL; input parquets are existence-checked,
    never silently skipped; BOTH speed sources missing is FATAL.
  * geo_shapes is capped by serialized bytes (GEOSHAPES_MAX_BYTES) so it
    always fits inside one <=4 MB part — split_sql refuses to cut mid-table.
"""
import datetime as dt
import math
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MODEL, GTFS  # noqa: E402
import split_sql  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ROW_BUDGET = int(os.environ.get("DP_D1_ROW_BUDGET", "80000"))
# geo_shapes rows are ~3 KB of JSON each: at the old 10k-row cap the table
# alone was 6.8 MB — bigger than one D1 part, which forced split_sql to cut
# mid-table. Cap the TABLE by serialized bytes instead so every table stays
# splittable at table boundaries (fix-plan F3).
GEOSHAPES_MAX_BYTES = 3_400_000


def esc(v):
    """SQL literal. None/NaN/±Inf -> NULL; strings quoted; floats rounded."""
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "NULL"
    if not math.isfinite(f):    # NaN and ±Inf must never reach the SQL
        return "NULL"
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


def load_speed(model_dir, base):
    """Traversal-first loader for one speed table.

    Returns (DataFrame, source_tag). Falls back to the ping-pair *_real
    parquet with a printed warning when the traversal file is missing or
    empty; FATAL when neither exists (an empty speed layer must never be
    exported silently).
    """
    trav = Path(model_dir) / f"{base}.parquet"
    real = Path(model_dir) / f"{base}_real.parquet"
    if trav.exists():
        df = pd.read_parquet(trav)
        if len(df):
            return df, "traversal"
        print(f"WARNING: {trav.name} exists but is empty; trying {real.name}")
    if real.exists():
        why = "empty" if trav.exists() else "missing"
        print(f"WARNING: {trav.name} {why} — falling back to ping-pair "
              f"{real.name} (rows stamped source='ping-pair')")
        return pd.read_parquet(real), "ping-pair"
    sys.exit(f"export_d1: !! neither {trav.name} nor {real.name} exists in "
             f"{model_dir} — run the model chain (pipeline/overnight.py) "
             f"before exporting.")


def main():
    total_budget = ROW_BUDGET
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()

    out = ["PRAGMA foreign_keys=OFF;"]
    written = 0
    sources = {}

    # ---- city_hour_real (24 rows) ----
    ch, sources["city_hour"] = load_speed(MODEL, "city_hour")
    ch = ch.dropna(subset=["hour_ist"])           # NULL PK = silent rot
    written += emit_table(out, "city_hour_real",
        "(hour_ist INTEGER PRIMARY KEY, speed_kmh REAL, n INTEGER, source TEXT)",
        ["hour_ist", "speed_kmh", "n", "source"],
        [(r.hour_ist, r.speed_kmh, r.n, sources["city_hour"])
         for r in ch.itertuples()])
    print(f"city_hour_real: {len(ch)} rows (source={sources['city_hour']})")

    # ---- route_hour_real (traversal-first, evidence-ranked to budget) ----
    rh, sources["route_hour"] = load_speed(MODEL, "route_hour")
    rh = rh.dropna(subset=["route_id", "hour_ist"])
    rh = rh[rh.n >= 3]                     # drop noise cells (NaN n fails too)
    rh = rh.sort_values("n", ascending=False)
    take = max(1000, total_budget // 2 - 2000)            # ~half the budget
    rh = rh.head(take)
    written += emit_table(out, "route_hour_real",
        "(route_id TEXT, hour_ist INTEGER, speed_kmh REAL, n INTEGER,"
        " source TEXT, PRIMARY KEY(route_id, hour_ist))",
        ["route_id", "hour_ist", "speed_kmh", "n", "source"],
        [(str(r.route_id), r.hour_ist, r.speed_kmh, r.n,
          sources["route_hour"]) for r in rh.itertuples()], batch=250)
    print(f"route_hour_real: {len(rh)} rows (source={sources['route_hour']}, "
          f"budget-capped, n>=3)")

    # ---- route_speed_real ----
    rs, sources["route_speed"] = load_speed(MODEL, "route_speed")
    rs = rs.dropna(subset=["route_id"])
    written += emit_table(out, "route_speed_real",
        "(route_id TEXT PRIMARY KEY, speed_kmh REAL, n INTEGER, source TEXT)",
        ["route_id", "speed_kmh", "n", "source"],
        [(str(r.route_id), r.speed_kmh, r.n, sources["route_speed"])
         for r in rs.itertuples()])
    print(f"route_speed_real: {len(rs)} rows (source={sources['route_speed']})")

    # ---- geo_shapes (one row per route, thinned coords, BYTE-capped) ----
    rows_geo = []
    if all((GTFS / f).exists() for f in
           ("shapes.txt", "trips.txt", "routes.txt")):
        import json as _json
        shapes = {}
        for sid, g in pd.read_csv(GTFS / "shapes.txt").groupby(
                "shape_id", sort=False):
            g = g.sort_values("shape_pt_sequence")
            shapes[str(sid)] = list(zip(g.shape_pt_lat.astype(float),
                                        g.shape_pt_lon.astype(float)))
        rshapes = {}
        for r in pd.read_csv(GTFS / "trips.txt", dtype=str).itertuples():
            rshapes.setdefault(str(r.route_id), str(r.shape_id))
        names = pd.read_csv(GTFS / "routes.txt", dtype=str)
        names["route_id"] = names.route_id.astype(str)
        nmap = dict(zip(names.route_id,
                        names.route_short_name.fillna(names.route_long_name)
                                            .fillna("")))
        for rid, sid in rshapes.items():
            pts = shapes.get(sid)
            if not pts:
                continue
            stride = max(1, len(pts) // 120)
            coords = [[round(lng, 5), round(lat, 5)]
                      for lat, lng in pts[::stride]]
            if len(coords) < 2:
                continue
            rows_geo.append((rid, str(nmap.get(rid, rid))[:60],
                             _json.dumps(coords)))
        # keep only routes that survived the route_hour budget, highest
        # evidence first, then cap the whole table by serialized bytes so it
        # always fits in a single <=4 MB part (split_sql cuts at table
        # boundaries only — a 6.8 MB geo_shapes table cannot be sharded).
        keep_n = dict(zip(rh.route_id.astype(str), rh.n))
        rows_geo = [r for r in rows_geo if r[0] in keep_n]
        rows_geo.sort(key=lambda r: -float(keep_n.get(r[0], 0)))
        rows_geo = rows_geo[: total_budget // 8]
        kept, used = [], 0
        for row in rows_geo:
            b = len(row[0]) + len(row[1]) + len(row[2]) + 40  # INSERT overhead
            if used + b > GEOSHAPES_MAX_BYTES and kept:
                print(f"geo_shapes: byte cap {GEOSHAPES_MAX_BYTES} hit — kept "
                      f"{len(kept)}/{len(rows_geo)} highest-evidence routes")
                break
            kept.append(row)
            used += b
        rows_geo = kept
    else:
        print(f"WARNING: GTFS shapes/trips/routes missing under {GTFS} — "
              f"skipping geo_shapes entirely")
    written += emit_table(out, "geo_shapes",
        "(route_id TEXT PRIMARY KEY, name TEXT, coords TEXT)",
        ["route_id", "name", "coords"], rows_geo, batch=8)
    geo_mb = sum(len(r[2]) for r in rows_geo) / 1e6
    print(f"geo_shapes: {len(rows_geo)} routes ({geo_mb:.2f} MB coords)")

    # ---- wait_headway (ping-derived 04g table; evidence-ranked remainder) --
    wh_path = MODEL / "wait_headway_real.parquet"
    written_wh = 0
    if wh_path.exists():
        wh = pd.read_parquet(wh_path)
        wh = wh.dropna(subset=["route_id", "hour_ist"])   # keys only; value
        # NaNs survive as NULL cells via esc() (fix-plan F: None|NaN|Inf->NULL)
        remaining = max(2000, total_budget - written - 3000)
        wh = wh.sort_values("n_services_per_day",
                            ascending=False).head(remaining)
        written_wh = emit_table(out, "wait_headway",
            "(route_id TEXT, hour_ist INTEGER, headway_s REAL,"
            " services_per_day REAL, source TEXT,"
            " PRIMARY KEY(route_id, hour_ist))",
            ["route_id", "hour_ist", "headway_s", "services_per_day",
             "source"],
            [(str(r.route_id), r.hour_ist, r.headway_s,
              r.n_services_per_day, "ping-pair") for r in wh.itertuples()],
            batch=150)
        print(f"wait_headway: {len(wh)} route-hour cells (source=ping-pair, "
              f"budget-capped)")
    else:
        print(f"WARNING: {wh_path.name} missing — wait_headway table skipped "
              f"(edge wait-time estimates degrade)")
    written += written_wh

    # ---- meta (`family` row: which corpus the speed tables came from) ------
    tags = [sources["city_hour"], sources["route_hour"],
            sources["route_speed"]]
    family = tags[0] if all(t == tags[0] for t in tags) else "mixed"
    meta = {
        "version": "v2-full-month",
        "exported_utc": stamp,
        "family": family,
        "row_budget": str(total_budget),
    }
    # +1 = the rows_written row itself, so the recorded count includes meta
    meta["rows_written"] = str(written + len(meta) + 1)
    out.append("DROP TABLE IF EXISTS meta;")
    out.append("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);")
    for k, v in meta.items():
        out.append(f"INSERT INTO meta VALUES ('{k}', '{v}');")
    written += len(meta)

    dst = ROOT / "docs" / "d1_export.sql"
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"TOTAL ROWS WRITTEN: {written} (budget {total_budget}) "
          f"{'OK' if written < 100000 else 'OVER FREE LIMIT!'}")
    print(f"sql -> {dst} ({dst.stat().st_size/1e6:.2f} MB)")

    # ---- F2: export+split atomic. split() re-reads the file from disk and
    # hard-fails unless it carries exactly the stamp written above, so the
    # parts can never be built from a stale d1_export.sql.
    parts = split_sql.split(dst, expect_stamp=stamp)
    print(f"parts -> {', '.join(p.name for p in parts)}")


if __name__ == "__main__":
    main()
