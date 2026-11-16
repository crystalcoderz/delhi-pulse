# Finalize the GCP full-month run:
#   pull artifacts -> integrate into repo -> deploy edge + D1 + HF -> smoke test
#   -> delete the processing VM.
#
# Required env (no defaults — set these before running):
#   $env:GCP_PROJECT   GCP project id
#   $env:GCP_ZONE      VM zone, e.g. "asia-south1-a"
#
# Usage (from repo root):   powershell -File deployment\finalize_gcp.ps1 [-KeepVm]
param([switch]$KeepVm)

$ErrorActionPreference = "Stop"
$proj = $env:GCP_PROJECT
$zone = $env:GCP_ZONE
$vm   = "delhi-pulse-proc"

Set-Location (Join-Path $PSScriptRoot "..")

function Step($n, $msg) { Write-Host "`n=== [$n] $msg ===" -ForegroundColor Cyan }
function Die($msg)   { Write-Host "FAILED: $msg" -ForegroundColor Red; exit 1 }

if (-not $proj) { Die 'set $env:GCP_PROJECT to the GCP project id' }
if (-not $zone) { Die 'set $env:GCP_ZONE to the VM zone' }

# 1) pull artifacts
Step 1 "pulling /opt/artifacts.tar.gz (~1 GB)"
gcloud compute scp --quiet --zone $zone --project $proj `
  "${vm}:/opt/artifacts.tar.gz" dev\artifacts.tar.gz
if ($LASTEXITCODE -ne 0) { Die "artifact pull" }

# 2) unpack into the repo (model/, data/clean/, docs/, workers/public/model.json*)
Step 2 "unpacking artifacts into repo"
tar -xzf dev\artifacts.tar.gz
if ($LASTEXITCODE -ne 0) { Die "unpack" }
Get-ChildItem model | Measure-Object | ForEach-Object { Write-Host "model files: $($_.Count)" }
Get-ChildItem docs\d1_part*.sql | Measure-Object | ForEach-Object { Write-Host "d1 parts: $($_.Count)" }

# 3) commit tracked deltas (model/ and data/ are gitignored; docs + edge assets track)
Step 3 "committing docs + edge model updates"
git add -A docs workers
# author identity comes from the operator's own git config — never hard-code it here
git commit -m "full-month model (Oct 1-31): 30 traversal days retrained, refreshed d1 exports, edge model.json"
if ($LASTEXITCODE -eq 0) { git push }

# 4) edge deploy
Step 4 "deploying edge worker (new model.json.gz asset)"
Push-Location workers
npx --yes wrangler@latest deploy
$edge = $LASTEXITCODE
Pop-Location
if ($edge -ne 0) { Die "wrangler deploy" }

# 5) D1 refresh
Step 5 "refreshing Cloudflare D1 (remote)"
foreach ($p in "docs\d1_part1.sql", "docs\d1_part2.sql", "docs\d1_part3.sql") {
  npx --yes wrangler@latest d1 execute delhi-pulse --remote --file $p
  if ($LASTEXITCODE -ne 0) { Die "d1 import $p" }
}

# 6) Hugging Face refresh
Step 6 "refreshing HF dataset (model tables)"
& ".venv\Scripts\python.exe" deployment\upload_hf.py
if ($LASTEXITCODE -ne 0) { Die "HF upload" }

# 7) smoke test the live edge
Step 7 "smoke test"
$h = curl.exe -sS --max-time 60 "https://delhi-pulse.prayaas.workers.dev/api/health"
Write-Host "health: $h"
$j = curl.exe -sS --max-time 90 "https://delhi-pulse.prayaas.workers.dev/api/journeys?from=28.6673,77.2285&to=28.5494,77.2514&max=1"
Write-Host "journey: $j".Substring(0, [Math]::Min(220, $j.Length))

# 8) VM cleanup
if ($KeepVm) {
  Write-Host "`nVM kept (-KeepVm). Delete later with: gcloud compute instances delete $vm --zone $zone --project $proj"
} else {
  Step 8 "deleting VM (run complete)"
  gcloud compute instances delete $vm --zone $zone --project $proj --quiet
  if ($LASTEXITCODE -ne 0) { Die "VM delete (delete manually!)" }
}

Write-Host "`nFINALIZE COMPLETE" -ForegroundColor Green
