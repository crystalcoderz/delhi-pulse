"""Overnight: full-month stop-level model.
1) stage-03 every day with pings but no traversals (8-way parallel each)
2) refresh chain: 04f -> 05 -> 04 -> 04g -> holdouts -> report -> d1 export
Logs to docs/overnight.log; failures recorded and skipped.
"""
import functools
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CLEAN  # freshness guard + corpus glob root

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = ROOT / "docs" / "overnight.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(*args, ok_codes=(0,)):
    cmd = [PY, *args]
    log(f":: {' '.join(a for a in args[:3])} ...")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT))
    dt = (time.time() - t0) / 60
    if r.returncode not in ok_codes:
        log(f"!! FAILED rc={r.returncode} after {dt:.1f} min: {args}")
        return False
    log(f"ok ({dt:.1f} min)")
    return True


def main():
    log("=== overnight start ===")
    days_have = {p.stem.replace("pings_", "")
                 for p in (ROOT / "data/clean").glob("pings_*.parquet")}
    days_done = {p.stem.replace("traversals_", "")
                 for p in (ROOT / "data/clean").glob("traversals_*.parquet")}
    # real days only (skip synth-named leftovers)
    todo = sorted(d for d in days_have - days_done if d.startswith("2026_10"))
    EXCLUDE = {"2026_10_16"}   # pathologically slow day-16 (archive corruption zone) - investigate separately
    todo = [d for d in todo if d not in EXCLUDE]
    log(f"days with pings: {len(days_have)}, traversals done: {len(days_done)}, "
        f"todo: {len(todo)} -> {todo} (excluded: {sorted(EXCLUDE & days_have)})")
    # full-width projection. Days run in DP_DAYS_PARALLEL streams (default 2)
    # so the 12 cores stay busy through each day's serial load/write phases.
    # Same computation as sequential — no effect on model quality.
    w03 = int(os.environ.get(
        "DP_STREAM03_WORKERS",
        max(4, (os.cpu_count() or 12)
             // max(1, int(os.environ.get("DP_DAYS_PARALLEL", "2"))))))
    par = max(1, int(os.environ.get("DP_DAYS_PARALLEL", "2")))
    log(f"[03] streams: {par}, workers/day: {w03}, todo: {len(todo)}")

    import concurrent.futures as cf

    def day_run(d):
        ok = run("pipeline/03_project_trips.py", "--day", d,
                 "--workers", str(w03))
        return d, ok

    if par > 1 and len(todo) > 1:
        with cf.ThreadPoolExecutor(max_workers=par) as ex:
            stage3_fail = [d for d, ok in ex.map(day_run, todo) if not ok]
    else:
        stage3_fail = [d for d in todo if not day_run(d)[1]]
    if stage3_fail:
        log(f"stage-03 FAILED days (skipped; rerunnable): {stage3_fail}")

    log("=== refresh chain ===")
    # any failure here means downstream stages would silently consume the
    # PREVIOUS night's model files and stamp them fresh — the exact bug that
    # shipped a stale 18-day edge_hour as v1.1 on 16 Nov. Make it fatal.
    chain_fail = []
    stages = [
        ("pipeline/04f_pings_speeds.py", "--all"),
        ("pipeline/05_ping_model.py",),
        ("pipeline/04_train_model.py",),
        ("pipeline/04g_headways.py",),
        ("validation/hold_pings.py",),
        ("validation/holdout_journeys.py",),
        ("validation/build_report.py",),
        ("deployment/export_d1.py",),
        ("deployment/split_sql.py",),
    ]
    for st in stages:
        if not run(*st):
            chain_fail.append(st[0])
            break           # stop before anything downstream gets a new stamp
    if chain_fail:
        log(f"!! MODEL CHAIN FAILED at {chain_fail[0]} — NOTHING was exported; "
            f"model/ and docs/ still hold the last good run. Fix and rerun.")
        sys.exit(1)

    # freshness guard: exports must never be newer than the corpus they claim
    import datetime as _dt
    import json as _json
    _meta = _json.loads((ROOT / "model/meta.json").read_text(encoding="utf-8"))
    newest_in = max(p.stat().st_mtime for p in CLEAN.glob("traversals_*.parquet"))
    if (ROOT / "model/meta.json").stat().st_mtime < newest_in:
        log("!! freshness check failed: meta.json predates the newest traversal "
            "file — a stage failed silently. Refusing to declare complete.")
        sys.exit(1)
    if len(_meta.get("days", [])) != len(list(CLEAN.glob('traversals_*.parquet'))):
        log(f"!! meta covers {len(_meta.get('days', []))} days but corpus has "
            f"{len(list(CLEAN.glob('traversals_*.parquet')))} files.")
        sys.exit(1)
    log("=== overnight complete — run finalize_month.py --deploy when ready ===")


if __name__ == "__main__":
    main()
