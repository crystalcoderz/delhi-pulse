"""Upload model artifacts + validation results to Hugging Face datasets."""
import functools
import json
import sys
import time
from pathlib import Path

print = functools.partial(print, flush=True)

from huggingface_hub import HfApi, create_repo, upload_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import os as _os
TOKEN = _os.environ.get("HF_TOKEN", "")  # set via env, never commit
USER = "PRAYAASOG"
REPO = f"{USER}/delhi-pulse-model"
ROOT = Path(__file__).resolve().parent.parent

api = HfApi(token=TOKEN)

print("== creating repo ==")
try:
    create_repo(REPO, repo_type="dataset", private=False, exist_ok=True)
    print(f"  {REPO} ready")
except Exception as e:
    print(f"  repo exists or error: {e}")

uploads = []

# ---- model tables ----
for f in sorted((ROOT / "model").glob("*.parquet")):
    uploads.append((f, f"model/{f.name}"))
for f in sorted((ROOT / "model").glob("*.json")):
    uploads.append((f, f"model/{f.name}"))

# ---- daily speed profiles ----
for f in sorted((ROOT / "data/clean").glob("speeds_*.parquet")):
    uploads.append((f, f"speeds/{f.name}"))
for f in sorted((ROOT / "data/clean").glob("speedsagg_*.parquet")):
    uploads.append((f, f"speeds/{f.name}"))

# ---- daily cleaning stats ----
for f in sorted((ROOT / "data/clean").glob("stats_*.json")):
    uploads.append((f, f"stats/{f.name}"))

# ---- validation results ----
for f in sorted((ROOT / "model").glob("holdout*.json")):
    uploads.append((f, f"report/{f.name}"))
for f in sorted((ROOT / "docs").glob("*.json")):
    uploads.append((f, f"report/{f.name}"))

# ---- sample: 1 day of traversals (proof of pipeline) ----
for prefix in ("traversals_2026_10_13", "passages_2026_10_13"):
    f = ROOT / "data/clean" / f"{prefix}.parquet"
    if f.exists():
        uploads.append((f, f"sample/{prefix}.parquet"))

for f in sorted((ROOT / "data/clean").glob("pings_2026_10_13.parquet")):
    uploads.append((f, f"sample/pings_2026_10_13.parquet"))

# ---- technical note ----
uploads.append((ROOT / "docs/technical-note.md", "report/technical-note.md"))

print(f"\n== uploading {len(uploads)} files ==")
size_total = sum(f.stat().st_size for f, _ in uploads)
print(f"  total size: {size_total/1e6:.1f} MB\n")

for i, (local_path, repo_path) in enumerate(uploads):
    try:
        size_mb = local_path.stat().st_size / 1e6
        print(f"  [{i+1}/{len(uploads)}] {repo_path} ({size_mb:.1f} MB)", end=" ")
        t0 = time.time()
        upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=repo_path,
            repo_id=REPO,
            repo_type="dataset",
        )
        print(f"OK ({time.time()-t0:.0f}s)")
    except Exception as e:
        print(f"FAILED: {e}")

print(f"\n== done: huggingface.co/datasets/{REPO} ==")
