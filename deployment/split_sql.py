"""Split docs/d1_export.sql into parts D1 accepts (exec size limit).

Contract (fix-plan F3):
  * Parts are cut ONLY at table boundaries — every part starts with a
    DROP TABLE statement and each table's DROP+CREATE+INSERTs stay together,
    so any single part imports cleanly into a fresh D1 database on its own.
    A part ends only on the last complete statement of its last table.
  * Parts are <= 4 MB (wrangler d1 exec size limit).
  * A single table larger than the cap is FATAL here — it must be shrunk at
    export time (export_d1.GEOSHAPES_MAX_BYTES / row budget), never split
    mid-table, because a mid-table cut breaks per-part importability.
  * The exported_utc stamp inside the file is asserted against
    `expect_stamp`; export_d1.main() passes the stamp it just wrote, so
    export+split are atomic and a stale docs/d1_export.sql can never be
    sharded by accident (fix-plan F2).

Standalone use:  python deployment/split_sql.py [expected_exported_utc_stamp]
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / "docs" / "d1_export.sql"
PART_LIMIT = 4_000_000           # bytes of SQL per part
PRAGMA = "PRAGMA foreign_keys=OFF;"

_STAMP_RE = re.compile(
    r"INSERT INTO meta VALUES \('exported_utc', '([^']+)'\)")
_FAMILY_RE = re.compile(
    r"INSERT INTO meta VALUES \('family', '([^']+)'\)")


def _fail(msg):
    sys.exit(f"split_sql: !! {msg}")


def _table_of(drop_stmt):
    return drop_stmt.lstrip().removeprefix(
        "DROP TABLE IF EXISTS ").split(";", 1)[0].strip()


def split(src=DEFAULT_SRC, out_dir=None, expect_stamp=None, limit=PART_LIMIT):
    """Shard a d1_export.sql file into d1_partN.sql files.

    Returns the list of written part paths. Fails loudly (sys.exit) on a
    missing stamp, a stamp that does not match `expect_stamp`, or any single
    table too large for one part.
    """
    src = Path(src)
    text = src.read_text(encoding="utf-8")

    stamp_m = _STAMP_RE.search(text)
    if not stamp_m:
        _fail(f"{src.name} has no exported_utc meta row — it predates the "
              f"atomic export or is corrupt; refusing to split.")
    stamp = stamp_m.group(1)
    if expect_stamp is not None and stamp != expect_stamp:
        _fail(f"stale export: {src.name} carries exported_utc={stamp} but "
              f"this run wrote {expect_stamp}. Refusing to split.")
    family_m = _FAMILY_RE.search(text)
    family = family_m.group(1) if family_m else "unknown"

    # -- parse into statements (export_d1 emits one statement per line) ----
    statements, buf = [], []
    for line in text.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))

    # -- group into table blocks: a block starts at its DROP TABLE and owns
    # every statement until the next DROP TABLE. Statements before the first
    # DROP TABLE are preamble (PRAGMA etc.) and are re-emitted per part.
    pre, blocks, cur = [], [], None
    for s in statements:
        if s.lstrip().startswith("DROP TABLE IF EXISTS "):
            cur = [s]
            blocks.append(cur)
        elif cur is None:
            pre.append(s)
        else:
            cur.append(s)
    if not blocks:
        _fail(f"{src.name} contains no DROP TABLE statements — "
              f"not a d1_export.sql file?")

    # -- pack WHOLE blocks into parts <= limit ------------------------------
    # header + PRAGMA overhead is accounted for so a full part never spills
    # past the limit once the part header line is written.
    overhead = len(PRAGMA) + 160
    parts, packed, size = [], [], overhead
    for b in blocks:
        bsize = sum(len(s) + 1 for s in b)
        if bsize > limit - overhead:
            _fail(f"table {_table_of(b[0])} is {bsize/1e6:.2f} MB — bigger "
                  f"than one {limit/1e6:.0f} MB part. Shrink it in "
                  f"export_d1.py (GEOSHAPES_MAX_BYTES / DP_D1_ROW_BUDGET); "
                  f"splitting mid-table would break per-part importability.")
        if size + bsize > limit and packed:
            parts.append(packed)
            packed, size = [], overhead
        packed.append(b)
        size += bsize
    if packed:
        parts.append(packed)

    # -- write parts; purge stale parts left by an earlier, bigger export ---
    out_dir = Path(out_dir) if out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for old in sorted(out_dir.glob("d1_part*.sql")):
        old.unlink()
    for i, blocks_i in enumerate(parts, 1):
        body = [s for s in pre if s.strip() != PRAGMA] if i == 1 else []
        for b in blocks_i:
            body.extend(b)
        head = (f"-- delhi-pulse d1 part {i}/{len(parts)} "
                f"(family={family}, exported_utc={stamp})")
        part_text = "\n".join([head, PRAGMA] + body) + "\n"
        if not part_text.rstrip().endswith(";"):
            _fail(f"part {i} would not end on a complete statement — bug, "
                  f"aborting without writing.")
        f = out_dir / f"d1_part{i}.sql"
        f.write_text(part_text, encoding="utf-8")
        written.append(f)
        tables = ", ".join(_table_of(b[0]) for b in blocks_i)
        print(f"{f.name}: {f.stat().st_size/1e6:.2f} MB, tables: {tables}")
    note = "" if expect_stamp is not None else (
        " (stamp NOT asserted — normal use is via export_d1.py, "
        "or pass the stamp as argv[1])")
    print(f"split_sql: {len(written)} parts <= {limit/1e6:.0f} MB, "
          f"exported_utc={stamp}{note}")
    return written


if __name__ == "__main__":
    expect = sys.argv[1] if len(sys.argv) > 1 else None
    split(DEFAULT_SRC, expect_stamp=expect)
