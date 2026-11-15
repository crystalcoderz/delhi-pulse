"""Month finalize: refresh model over all days -> build report -> export D1
-> deploy worker to the edge. Run after the sandbox month-run completes.
Usage:  python pipeline/finalize_month.py [--deploy]
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(cmd, cwd=None, tail=3):
    print(f">> {' '.join(map(str, cmd))}")
    r = subprocess.run(list(cmd), cwd=str(cwd or ROOT))
    if r.returncode != 0:
        sys.exit(f"failed ({r.returncode}): {' '.join(map(str, cmd))}")


def main():
    deploy = "--deploy" in sys.argv

    run([PY, ROOT / "pipeline" / "refresh_model.py"])
    run([PY, ROOT / "validation" / "build_report.py"])
    run([PY, ROOT / "deployment" / "export_d1.py"])

    if deploy:
        run(["npx", "--yes", "wrangler@latest", "d1", "execute", "delhi-pulse",
             "--remote", "--file", ROOT / "docs" / "d1_export.sql"
             ], cwd=ROOT / "workers")
        run(["npx", "--yes", "wrangler@latest", "deploy"],
            cwd=ROOT / "workers")

        n_days = len(list((ROOT / "data/clean").glob("pings_*.parquet")))
        print(f"\nFINAL: {n_days} real October days -> edge model redeployed.")
        print("verify: https://delhi-pulse.prayaas.workers.dev/health")


if __name__ == "__main__":
    main()
