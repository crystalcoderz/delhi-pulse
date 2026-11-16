"""A1 regression suite: IST hour/day binning must be timezone-independent.

The original pipeline derived hour cells with
``hour(to_timestamp(ts_utc + 19800))``. DuckDB's to_timestamp returns
TIMESTAMPTZ and hour() renders it in the SESSION timezone, so on an
IST machine (Asia/Calcutta) the 19800-second offset got applied a second
time and every hour bucket landed 5.5 hours late. These tests pin the
integer-arithmetic replacement and cross-check the SQL expression against
the Python reference under BOTH session timezones.

Run:  python tests/test_time_bins.py   (also pytest-compatible)
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import duckdb  # noqa: E402

from common import (IST_OFFSET_S, dow_ist, dow_ist_expr,  # noqa: E402
                    hour_ist, hour_ist_expr)

# 2026-10-13 03:00:00 UTC == 08:30 IST, Tuesday (2026-10-13 is a Tuesday)
TS_0300Z = int(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc).timestamp())
TS_MONDAY_EARLY = int(datetime(2026, 10, 12, 3, 0,  # 2026-10-12 08:30 IST, Mon
                               tzinfo=timezone.utc).timestamp())
SWEEP = [0, 1, 19800, 1795999999, TS_0300Z - 7200, 1796000000,
         48 * 3600, 48 * 3600 + 19800] + [TS_0300Z + i * 997 for i in range(50)]


def test_0300_utc_is_0830_ist():
    assert hour_ist(TS_0300Z) == 8, "03:00Z must land in IST hour 8 (08:30)"


def test_dow_monday():
    # 2026-10-13 01:30 IST is still Monday 2026-10-12 20:00Z
    assert dow_ist(TS_MONDAY_EARLY) == 1
    assert dow_ist(TS_0300Z) == 2, "2026-10-13 is a Tuesday"


def test_sql_expr_matches_python_everywhere():
    """The SQL must equal the Python reference regardless of session tz —
    the exact failure mode that produced 5.5-hour-shifted hour bins."""
    for tz in ("Asia/Kolkata", "Etc/UTC", "America/New_York"):
        con = duckdb.connect()
        con.execute(f"SET TimeZone='{tz}'")
        rows = con.execute(f"""
            SELECT ts, {hour_ist_expr('ts')}, {dow_ist_expr('ts')}
            FROM (SELECT unnest({json.dumps(SWEEP)})::BIGINT AS ts)
        """).fetchall()
        for ts, h, d in rows:
            assert h == hour_ist(ts), f"tz={tz} ts={ts}: sql hour {h} != {hour_ist(ts)}"
            assert d == dow_ist(ts), f"tz={tz} ts={ts}: sql dow {d} != {dow_ist(ts)}"
        con.close()


def test_offset_value():
    assert IST_OFFSET_S == 5 * 3600 + 30 * 60


if __name__ == "__main__":
    test_0300_utc_is_0830_ist()
    test_dow_monday()
    test_sql_expr_matches_python_everywhere()
    test_offset_value()
    print("all time-bin tests OK")
