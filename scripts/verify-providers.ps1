# Live provider verification: discover models, then prove which actually serve.
#
# Two stages, both necessary. Zen lists `deepseek-v4-flash-free` and then
# returns "Model is unavailable" for it, so listing is not proof. And the
# converse: discovery reconciles the configured models against each provider's
# live listing, which is what catches a model that has been withdrawn — how Ox
# Alpha's removal was found on 2026-08-26.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run .\bootstrap.ps1 first."; exit 1 }

$port = if ($env:OE_MAX_PORT) { $env:OE_MAX_PORT } else { "8787" }

try {
  Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 5 | Out-Null
} catch {
  Write-Host "Broker not running. Start it:  .\scripts\start-broker.ps1"
  exit 1
}

# Smoke-testing every model on every provider is slow by design — each probe is
# a real completion — so the timeout is generous rather than optimistic.
$probes = Invoke-RestMethod -Method Post -TimeoutSec 600 `
  -Uri "http://127.0.0.1:$port/v1/oe-max/verify?check_tools=true"

$probes | ConvertTo-Json -Depth 10 | & $py (Join-Path $PWD "scripts\_print_probes.py")
