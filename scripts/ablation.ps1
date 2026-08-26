# Run the same task with each optional behaviour on and off, and report whether
# it helped. Requires the control plane (.\run.ps1).
#
#   .\scripts\ablation.ps1 --arms operators,island_policies --repeats 2
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run .\bootstrap.ps1 first."; exit 1 }
& $py (Join-Path $PWD "scripts\ablation.py") @args
