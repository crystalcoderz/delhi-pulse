"""Probe a W&B/CoreWeave sandbox: auth, machine spec, R2 bandwidth."""
import os
from dotenv import load_dotenv
load_dotenv()

from cwsandbox import AuthStrategy, Sandbox

with Sandbox.run(auth=AuthStrategy.WANDB) as sb:
    import time
    t0 = time.time()
    print("login + boot OK in %.1fs" % (time.time() - t0))
    r = sb.exec(["bash", "-lc",
                 "nproc; free -g | awk 'NR<3'; df -h / | tail -1; "
                 "cat /etc/os-release | head -2; python3 --version; "
                 "which 7z 7za sqlite3 curl git || true"]).result()
    print("---- spec ----")
    print(r.stdout)
    print(r.stderr or "(no stderr)")
    print("---- R2 speed (50 MB slice) ----")
    r2 = sb.exec(["bash", "-lc",
                  "curl -sS -o /dev/null -w 'bytes=%{size_download} speed_Bps=%{speed_download} time=%{time_total}\\n' "
                  "--max-time 60 -r 0-52428799 "
                  "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z"
                  ]).result()
    print(r2.stdout)
    print(r2.stderr or "(no stderr)")
