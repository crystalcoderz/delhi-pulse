"""Direct HuggingFace upload using the raw commit API (bypasses library bugs)."""
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

print("raw HF uploader", flush=True)

import os as _os
TOKEN = _os.environ.get("HF_TOKEN", "")  # set via env, never commit
REPO = "PRAYAASOG/delhi-pulse-model"
BASE = f"https://huggingface.co/api/datasets/{REPO}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ROOT = Path(__file__).resolve().parent.parent


def upload_small(path_in_repo, filepath):
    """Upload a single small file via the NDJSON commit API."""
    data = Path(filepath).read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    # NDJSON payload for the commit endpoint
    ndjson = ""
    ndjson += json.dumps({"key": "upload", "type": "file", "path": path_in_repo,
                          "content": base64.b64encode(data).decode(),
                          "encoding": "base64", "sha": sha}) + "\n"
    ndjson += json.dumps({"key": "msg", "type": "message",
                          "message": f"add {path_in_repo}"}) + "\n"

    r = requests.post(
        f"{BASE}/commit/main",
        headers={**HEADERS, "Content-Type": "application/x-ndjson"},
        data=ndjson,
    )
    return r


# ---- test with a tiny file first ----
test_file = ROOT / "model" / "meta.json"
r = upload_small("model/meta.json", test_file)
print(f"test upload: {r.status_code} {r.text[:200]}", flush=True)

if r.status_code == 200:
    print("SUCCESS — uploading all model files", flush=True)
    files = sorted((ROOT / "model").glob("*.json")) + \
            sorted((ROOT / "model").glob("*.parquet"))
    for f in files:
        # batch JSON files together (small)
        repo_path = f"model/{f.name}"
        rr = upload_small(repo_path, f)
        ok = "OK" if rr.status_code == 200 else f"FAIL {rr.status_code}: {rr.text[:100]}"
        print(f"  {repo_path}: {ok}", flush=True)
else:
    print(f"FAILED: {r.status_code} - {r.text[:300]}", flush=True)
    # try the overview endpoint to debug
    r2 = requests.get(f"{BASE}/tree/main", headers=HEADERS)
    print(f"tree: {r2.status_code} {r2.text[:200]}", flush=True)
    # try preupload with explicit auth
    r3 = requests.post(f"{BASE}/preupload/main",
                        headers={**HEADERS, "Content-Type": "application/json"},
                        json={"files": [{"path": "model/meta.json", "size": test_file.stat().st_size}]})
    print(f"preupload: {r3.status_code} {r3.text[:200]}", flush=True)
