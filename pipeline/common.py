"""Shared paths and config for the Delhi Pulse pipeline. OS-agnostic."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"          # per-day SQLite DBs extracted from the 7z
CLEAN = DATA / "clean"      # parquet outputs per stage
GTFS = DATA / "gtfs"        # static GTFS .txt files
MODEL = ROOT / "model"      # trained lookup tables
WEB = ROOT / "web"

ARCHIVE = Path("C:/Users/praya/Downloads/delhi-dtc-bus-gtfs-rt-2026-10.7z")
DATA_URL = "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z"
SUMS_URL = "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS"

# Delhi bounding box (loose)
BBOX = (28.30, 76.75, 28.95, 77.60)   # lat_min, lng_min, lat_max, lng_max

IST_OFFSET_S = 5 * 3600 + 30 * 60     # UTC -> IST

# Projection / cleaning knobs
MAX_IMPLIED_KMH = 120.0               # pings implying faster than this + far jump get dropped
JUMP_MIN_M = 150.0                    # only treat as jump if also displaced this far
NEAR_SHAPE_M = 75.0                   # ping must be within this of its trip's shape
NEAR_STOP_M = 150.0                   # stop must project within this of the trip shape
MIN_TRIP_PINGS = 6                    # trips with fewer clean pings are skipped
MIN_EDGE_S = 3.0
MAX_EDGE_S = 120 * 60.0


def ensure_dirs():
    for d in (DATA, RAW, CLEAN, GTFS, MODEL):
        d.mkdir(parents=True, exist_ok=True)


def ist_datetime_cols(col_ts: str = "ts_utc") -> str:
    """SQL expr: duckdb expression turning unix-seconds UTC into IST timestamp."""
    return f"(to_timestamp(({col_ts}::BIGINT + {IST_OFFSET_S})))"
