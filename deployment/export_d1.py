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
    meta = {
        "version": "v1-pings-real",
        "exported_utc": dt.datetime.utcnow().isoformat(),
    }
    for k, v in meta.items():
        out.append(f"INSERT OR REPLACE INTO meta VALUES ('{k}', '{v}');")
    dst = Path(__file__).resolve().parent.parent / "docs" / "d1_export.sql"
    dst.write_text("\n".join(out))
    print(f"sql -> {dst} ({dst.stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
