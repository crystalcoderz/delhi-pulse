"""One-command model refresh: 04f over new days -> 05 aggregate -> bake meta.
Idempotent; run after sandbox runs pull new pings parquets."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(stage, *args):
    cmd = [PY, str(ROOT / "pipeline" / stage), *args]
    print(f">> {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        sys.exit(f"{stage} failed with {r.returncode}")


if __name__ == "__main__":
    run("04f_pings_speeds.py", "--all")
    run("05_ping_model.py")
    print("model refreshed.")
