"""Quick health probe: what's running inside the sandbox, archive state."""
import functools
print = functools.partial(print, flush=True)
from dotenv import load_dotenv
load_dotenv()
from cwsandbox import AuthStrategy, Sandbox

sbs = Sandbox.list(tags=["delhi-pulse"], auth=AuthStrategy.WANDB).result()
print("active sandboxes:", [s.sandbox_id for s in sbs])
sb = sbs[0]

def run(cmd):
    r = sb.exec(["bash", "-lc", cmd], timeout_seconds=120).result()
    print(r.stdout or "", r.stderr or "")

run("ps aux | grep -E 'curl|python' | grep -v grep || echo 'no curl/python running'")
run("stat -c '%s bytes' /app/data/delhi-dtc-bus-gtfs-rt-2026-10.7z 2>/dev/null || echo 'archive missing'")
run("cd /app/data && grep delhi-dtc SHA256SUMS 2>/dev/null | sha256sum -c - 2>&1 || echo 'sha not verifiable'")
run("ls -la --block-size=M /app/data/clean/ 2>/dev/null; ls -la --block-size=M /app/data/raw/ 2>/dev/null | head -5")
