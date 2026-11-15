"""
Run the Delhi Pulse pipeline on real data inside a W&B/CoreWeave sandbox.

The sandbox downloads the 16 GB archive from Cloudflare R2 at datacenter speed
(~33 min measured), processes days one at a time, and ships only the small
parquet outputs back to the laptop. Raw day DBs are deleted after cleaning.

Usage:
  python pipeline/sandbox_run.py --days 2026_10_13                 # one day test
  python pipeline/sandbox_run.py --week 1                           # Oct 13-19
  python pipeline/sandbox_run.py --days 2026_10_13 2026_10_14 ...   # several

Notes:
  - Stage 03 (projection) also needs the real static GTFS in data/gtfs/;
    until Transport Stack access arrives, the runner stops after stage 02 and
    pulls the cleaned pings (still the 124 GB -> GBs crux step).
"""
import argparse
import functools
import sys
import time
from pathlib import Path

print = functools.partial(print, flush=True)  # unbuffered progress everywhere

from dotenv import load_dotenv
load_dotenv()

from cwsandbox import AuthStrategy, Sandbox

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_URL = "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z"
SUMS_URL = "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS"

SHIP_FILES = [
    "requirements.txt",
    "pipeline/common.py",
    "pipeline/01_extract_day.py",
    "pipeline/02_clean_pings.py",
    "pipeline/03_project_trips.py",
    "pipeline/04_train_model.py",
]

WEEK1 = [f"2026_10_{d}" for d in range(13, 20)]


def ts():
    return time.strftime("%H:%M:%S")


def run(sb, cmd, timeout=None, tail=40):
    r = sb.exec(["bash", "-lc", cmd], cwd="/app", timeout_seconds=timeout).result()
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        tail_lines = "\n".join(out.splitlines()[-tail:])
        print(f"[{ts()}] {tail_lines}")
    if err:
        print(f"[{ts()}] stderr: {err[-1200:]}")
    return out, err, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=[])
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--attach", action="store_true",
                    help="attach to the existing delhi-pulse sandbox instead of creating one")
    ap.add_argument("--cleanup-only", action="store_true",
                    help="stop+delete all our sandboxes and exit")
    a = ap.parse_args()
    days = a.days if a.days else []
    if a.week == 1:
        days = WEEK1
    if not days and not a.cleanup_only and not a.skip_download:
        days = ["2026_10_13"]  # default: single-day validation

    if a.cleanup_only:
        ours = Sandbox.list(tags=["delhi-pulse"],
                             auth=AuthStrategy.WANDB).result()
        for s in ours:
            print(f"stopping {s.sandbox_id}")
            s.stop()
        print(f"cleaned {len(ours)} sandbox(es)")
        return

    t0 = time.time()
    ours = Sandbox.list(tags=["delhi-pulse"],
                         auth=AuthStrategy.WANDB).result()
    created = False
    if a.attach:
        if not ours:
            print(f"[{ts()}] no active delhi-pulse sandbox to attach to; exiting")
            return
        sb = ours[0]
        sb.start().result()
        print(f"[{ts()}] attached to {sb.sandbox_id} "
              f"(status: {sb.status})")
    else:
        name = f"delhi-pulse-{int(time.time())}"
        print(f"[{ts()}] creating sandbox '{name}' "
              f"(requests: 2 CPU / 3Gi, fail-fast budgets) ...")
        resources = {"requests": {"cpu": "2", "memory": "3Gi"}}
        sb = Sandbox.run(auth=AuthStrategy.WANDB, tags=["delhi-pulse"],
                         display_name=name,
                         max_lifetime_seconds=6 * 3600,
                         create_retry_budget_seconds=900,
                         poll_retry_budget_seconds=900,
                         resources=resources)
        created = True
    print(f"[{ts()}] sandbox ready in {time.time()-t0:.0f}s -> {sb.sandbox_id}")

    try:
        # ---- ship the repo ----
        files = {f"/app/{rel}": (ROOT / rel).read_bytes() for rel in SHIP_FILES}
        sb.write_files(files).result()
        print(f"[{ts()}] shipped {len(files)} files -> /app")

        # ---- deps ----
        _, err, _ = run(sb, "cd /app && pip install -q -r requirements.txt"
                          " && python -c 'import duckdb, pandas, py7zr; print(\"deps OK\")'",
                        timeout=600, tail=2)

        # ---- archive download (idempotent: resumes, verifies, skips if complete) ----
        if not a.skip_download:
            run(sb, "mkdir -p /app/data && cd /app/data; "
                    f"curl -L -C - -sS --retry 10 --retry-delay 5 "
                    f"-o delhi-dtc-bus-gtfs-rt-2026-10.7z {ARCHIVE_URL} "
                    "|| echo 'curl done/rc noted'; "
                    f"curl -sS -o SHA256SUMS {SUMS_URL}; "
                    "grep delhi-dtc SHA256SUMS | sha256sum -c - "
                    "|| echo 'SHA NOT VERIFIED'; "
                    "ls -la --block-size=M delhi-dtc-bus-gtfs-rt-2026-10.7z",
                timeout=3600 * 2, tail=4)

        # ---- per-day processing ----
        for day in days:
            d0 = time.time()
            run(sb, f"cd /app && python pipeline/01_extract_day.py --day {day}",
                timeout=1800, tail=3)
            run(sb, f"cd /app && python pipeline/02_clean_pings.py --day {day}",
                timeout=3600, tail=6)
            gtfs = sb.exec(["bash", "-lc", "test -f /app/data/gtfs/stops.txt"]).result()
            if (gtfs.returncode or 0) == 0:
                run(sb, f"cd /app && python pipeline/03_project_trips.py --day {day}",
                    timeout=3600, tail=4)
            else:
                print(f"[{ts()}] {day}: no GTFS yet -> skipping stage 03 (03 needs the "
                      f"static GTFS from Transport Stack)")
            run(sb, "rm -f /app/data/raw/*.db; df -h /app | tail -1; "
                    "ls -la --block-size=M /app/data/clean/ | tail -5", tail=6)
            # pull artifacts back
            remote = sb.read_files([f"/app/data/clean/pings_{day}.parquet"]).result()
            for path, blob in remote.items():
                local = ROOT / "data/clean" / path.rsplit("/", 1)[-1]
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(blob)
                print(f"[{ts()}] pulled {path} -> {local} "
                      f"({len(blob)/1e6:.1f} MB)")
            # try traversals too if produced
            remote = sb.read_files([f"/app/data/clean/traversals_{day}.parquet"]).result()
            for path, blob in remote.items():
                (ROOT / "data/clean" / path.rsplit("/", 1)[-1]).write_bytes(blob)
                print(f"[{ts()}] pulled {path} ({len(blob)/1e6:.1f} MB)")
            print(f"[{ts()}] ==== day {day} done in {(time.time()-d0)/60:.1f} min ====")
    finally:
        if created:
            print(f"[{ts()}] stopping sandbox ...")
            try:
                sb.stop()
                print(f"[{ts()}] sandbox stopped "
                      f"(total run: {(time.time()-t0)/60:.0f} min)")
            except Exception as e:
                print(f"[{ts()}] stop failed ({type(e).__name__}) - run "
                      f"--cleanup-only later; sandbox dies by TTL anyway")
        else:
            print(f"[{ts()}] attached sandbox left running (TTL will reap it); "
                  f"use --cleanup-only to stop it now")


if __name__ == "__main__":
    sys.exit(main())
