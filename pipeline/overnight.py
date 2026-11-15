"""Overnight: full-month stop-level model.
1) stage-03 every day with pings but no traversals (8-way parallel each)
2) refresh chain: 04f -> 05 -> 04 -> 04g -> holdouts -> report -> d1 export
Logs to docs/overnight.log; failures recorded and skipped.
"""
import functools
import subprocess
import sys
import time
from pathlib import Path

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
    log(f"days with pings: {len(days_have)}, traversals done: {len(days_done)}, "
        f"todo: {len(todo)} -> {todo}")
    for d in todo:
        run("pipeline/03_project_trips.py", "--day", d)

    log("=== refresh chain ===")
    run("pipeline/04f_pings_speeds.py", "--all")
    run("pipeline/05_ping_model.py")
    run("pipeline/04_train_model.py")
    run("pipeline/04g_headways.py")
    run("validation/hold_pings.py")
    run("validation/build_report.py")
    run("deployment/export_d1.py")
    run("deployment/split_sql.py")
    log("=== overnight complete — run finalize_month.py --deploy when ready ===")


if __name__ == "__main__":
    main()
