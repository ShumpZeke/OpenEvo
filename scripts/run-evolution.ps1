# Terminal 2: run evolution through the broker.
#   .\scripts\run-evolution.ps1 -Task function_minimization -Profile max -Iterations 20
param(
  [string]$Task = "function_minimization",
  [string]$Profile = "max",
  [int]$Iterations = 20,
  [string]$Output = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD

$cfg = switch ($Profile) {
  "max"   { "configs/oe_max/evolution.yaml" }
  # Baseline arm: same model, same settings, but talking to the provider
  # directly instead of through the broker. That isolates the system from the
  # model, which is what makes the comparison meaningful.
  "stock" { "configs/oe_max/stock_baseline.yaml" }
  default { $Profile }                       # an explicit config path
}

$prog = Join-Path "examples" $Task | Join-Path -ChildPath "initial_program.py"
$eval = Join-Path "examples" $Task | Join-Path -ChildPath "evaluator.py"
foreach ($f in @($prog, $eval, $cfg)) {
  if (-not (Test-Path $f)) { Write-Error "missing: $f"; exit 1 }
}

if (-not $Output) {
  $Output = Join-Path "runs" "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$Task-$Profile"
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null

# Fail fast with a clear message rather than a confusing connection error.
$port = if ($env:OE_MAX_PORT) { $env:OE_MAX_PORT } else { "8787" }
if ($Profile -eq "max") {
  try { Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 5 | Out-Null }
  catch { Write-Error "The broker is not running. Start it: .\scripts\start-broker.ps1"; exit 1 }
}

# Run through the instrumented entrypoint, not `openevolve-run.py`.
#
# This script used to exec the plain upstream CLI, which installs no telemetry
# at all - and every OE_MAX_* feature is installed BY that telemetry. So
# setting OE_MAX_OPERATORS=1 against the old command set an environment
# variable that nothing ever read: no operator steering, no attribution, no
# multi-offspring, no verification, no bandit. The run succeeded and the
# feature silently did not happen, which is the worst way for a flag to fail.
#
# The entrypoint requires a run id, so generate one when the caller has not.
if (-not $env:EVOLUTION_RUN_ID) {
  $env:EVOLUTION_RUN_ID = "run_$(Get-Date -Format 'yyyyMMddHHmmss')_$PID"
}
if (-not $env:EVOLUTION_TELEMETRY) { $env:EVOLUTION_TELEMETRY = "1" }
# Without this the run log silently loses lines on Windows rather than mangling
# them: the child inherits the console code page, `logging` cannot encode the
# character, and the handler drops the whole record. Upstream marks "New best
# solution found" with a star and writes score changes with an arrow, so the
# discarded lines are the ones worth having. Measured: 0 bytes without, 48 with.
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:EVOLUTION_EVENT_LOG) { $env:EVOLUTION_EVENT_LOG = Join-Path $Output "events.ndjson" }

Write-Host "task=$Task profile=$Profile iterations=$Iterations"
Write-Host "config=$cfg"
Write-Host "output=$Output"
Write-Host "run_id=$($env:EVOLUTION_RUN_ID)"
Write-Host "events=$($env:EVOLUTION_EVENT_LOG)"
& $py -m control_plane.runner.entrypoint $prog $eval `
    --config $cfg --iterations $Iterations --output $Output
exit $LASTEXITCODE
