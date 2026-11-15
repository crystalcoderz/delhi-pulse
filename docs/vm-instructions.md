# Spin up the CoreWeave VM (one-time, ~10 minutes)

## 1. Create the VM in the CoreWeave portal

Log in to your CoreWeave account → **Virtual Servers** → create:

| Setting | Value | Why |
|---|---|---|
| Type | **Virtual Server** (CPU, NOT GPU) | DuckDB + LightGBM are CPU work; don't burn credits on a GPU |
| vCPU / RAM | 8–16 vCPU, 32–64 GB RAM | 124 GB dataset processed a day at a time |
| Disk | 200 GB SSD | 16 GB archive + ~5 GB per raw day + parquet outputs |
| Image | Ubuntu 22.04 / 24.04 | vm_setup.sh targets apt |
| SSH key | paste the key below | key already generated on your laptop |
| Public IP | enable | so we can ssh + judges can hit the API |

Your public key (already on this laptop at `C:\Users\praya\.ssh\id_ed25519_delhipulse.pub`):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBpRbFx6A+8ZNS71L0d2NMlOomgIGQYjrATiFMhV+J2 delhi-pulse-vm
```

## 2. Upload the project (from the laptop, in PowerShell)

```powershell
cd C:\Users\praya
tar -czf delhi-pulse.tgz delhi-pulse -C delhi-pulse --exclude .venv --exclude data/raw
scp -i $env:USERPROFILE\.ssh\id_ed25519_delhipulse delhi-pulse.tgz <VM_USER>@<VM_IP>:~
```
(`tar` ships with Windows 10+; the venv and raw data are excluded — the VM builds its own.)

## 3. Bootstrap + run

```bash
ssh -i ~/.ssh/id_ed25519_delhipulse <VM_USER>@<VM_IP>
tar xzf delhi-pulse.tgz && cd delhi-pulse
mkdir -p data/gtfs        # <- copy Transport Stack GTFS .txt files here first!
bash deployment/vm_setup.sh
```

`vm_setup.sh` installs everything, downloads the 16 GB straight from Cloudflare R2
(datacenter speed — minutes, not the ~day your browser needs), verifies the
SHA256 checksum, and processes a starter week. More days:

```bash
DAYS="2026_10_18 2026_10_19 ..." bash deployment/vm_setup.sh
# or without re-downloading: .venv/bin/python pipeline/01_extract_day.py --day 2026_10_18 etc.
```

## 4. Firewall

Open inbound TCP **8000** (API + UI) and **22** (ssh) on the VM's firewall/security group.

## Notes

- The VM holds the heavy stuff: raw day DBs get deleted after cleaning
  (`KEEP_RAW=1` to keep them). Only `model/*.parquet` (a few MB) matters long-term.
- Pull results back to the laptop anytime:
  `scp -i $key <vm>:/root/delhi-pulse/model/*.parquet C:\Users\praya\delhi-pulse\model\`
- Your browser download continues independently — it'll become a local fixture
  copy. Two copies of one archive is fine; two writers to ONE file is not.
