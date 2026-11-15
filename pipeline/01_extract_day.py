"""
Stage 1: extract a single day's SQLite DB from the non-solid 7z archive and index it.
Usage:
  python pipeline/01_extract_day.py --day 2026_10_15 [--archive path.7z]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from common import ensure_dirs, RAW, ARCHIVE


def extract(day: str, archive: Path) -> Path:
    ensure_dirs()
    if not archive.exists():
        print(f"[01] archive not found: {archive}")
        print("[01] copy the 7z into data/ or pass --archive")
        sys.exit(2)

    import py7zr
    target_name = f"bus_movements_{day}.db"
    out = RAW / target_name
    if out.exists() and out.stat().st_size > 1_000_000:
        print(f"[01] {out} already extracted, skipping (delete to re-extract)")
        return out

    print(f"[01] extracting {target_name} from {archive.name} ...")
    with py7zr.SevenZipFile(archive, mode="r") as z:
        names = z.getnames()
        hits = [n for n in names if target_name in n]
        if not hits:
            print(f"[01] {target_name} not in archive. Available entries: {len(names)}")
            sys.exit(2)
        z.extract(path=str(RAW), targets=hits)
    found = RAW / (hits[0].split("/")[-1])
    if found != out and found.exists():
        found.rename(out)
    print(f"[01] extracted -> {out} ({out.stat().st_size/1e9:.2f} GB)")
    return out


def index(db_path: Path):
    con = sqlite3.connect(db_path)
    print("[01] creating indexes (one-time, speeds cleaning 10x) ...")
    con.execute('CREATE INDEX IF NOT EXISTS idx_trip ON vehicle_feed(trip_id)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_veh_ts ON vehicle_feed(vehicle_id, "timestamp")')
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM vehicle_feed").fetchone()[0]
    t = con.execute("SELECT COUNT(DISTINCT trip_id) FROM vehicle_feed").fetchone()[0]
    con.close()
    print(f"[01] indexed: {n:,} pings, {t:,} trips in {db_path.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026_10_15")
    ap.add_argument("--archive", default=str(ARCHIVE))
    args = ap.parse_args()
    p = extract(args.day, Path(args.archive))
    index(p)
