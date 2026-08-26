# Run the same task on each of several routes and compare their mutation
# quality. Requires the broker (.\scripts\start-broker.ps1) and the control
# plane (.\run.ps1) to be up.
#
#   .\scripts\route-experiment.ps1 --routes x-preview-f-free,nemotron-3-ultra-free
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& "$PWD\.venv\Scripts\python.exe" scripts/route_experiment.py @args
