# Resume from the newest checkpoint of a run directory.
#   .\scripts\resume-evolution.ps1 runs\20260826-001122-function_minimization-max
#   .\scripts\resume-evolution.ps1 runs\my-run -Task circle_packing -Iterations 10
param(
  [Parameter(Mandatory = $true)][string]$Run,
  [int]$Iterations = 20,
  [string]$Task = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD

if (-not (Test-Path (Join-Path $Run "checkpoints"))) {
  Write-Error "no checkpoints in $Run"; exit 1
}

# The task used to be hardcoded to function_minimization, so resuming any other
# run silently continued it against the WRONG program and evaluator - the
# scores kept coming and measured a different problem. Recover it from the run
# directory's own name, which run-evolution builds as
# `runs/<timestamp>-<task>-<profile>`.
if (-not $Task) {
  $base  = Split-Path $Run -Leaf
  $guess = $base -replace '^\d{8}-\d{6}-', '' -replace '-(max|stock)$', ''
  if ($guess -and (Test-Path (Join-Path "examples" $guess))) {
    $Task = $guess
  } else {
    $Task = "function_minimization"
    Write-Warning "could not infer the task from '$base'; assuming $Task. Pass -Task NAME if that is wrong - resuming with the wrong evaluator produces scores for a different problem."
  }
}

$prog = Join-Path "examples" $Task | Join-Path -ChildPath "initial_program.py"
$eval = Join-Path "examples" $Task | Join-Path -ChildPath "evaluator.py"
foreach ($f in @($prog, $eval)) {
  if (-not (Test-Path $f)) { Write-Error "missing: $f (is -Task $Task right?)"; exit 1 }
}

$ck = Get-ChildItem (Join-Path $Run "checkpoints") -Filter "checkpoint_*" -Directory -ErrorAction SilentlyContinue |
      ForEach-Object { [int]($_.Name -replace 'checkpoint_', '') } | Sort-Object | Select-Object -Last 1
if (-not $ck) { Write-Error "no checkpoint found in $Run"; exit 1 }

# Through the instrumented entrypoint, not `openevolve-run.py`. The plain CLI
# installs no telemetry, and every OE-MAX feature is installed BY that
# telemetry. See HANDOFF section 3.11.
if (-not $env:EVOLUTION_RUN_ID) {
  $env:EVOLUTION_RUN_ID = "run_$(Get-Date -Format 'yyyyMMddHHmmss')_$PID"
}
if (-not $env:EVOLUTION_TELEMETRY) { $env:EVOLUTION_TELEMETRY = "1" }
if (-not $env:EVOLUTION_EVENT_LOG) { $env:EVOLUTION_EVENT_LOG = Join-Path $Run "events.ndjson" }
$checkpoint = Join-Path $Run "checkpoints" | Join-Path -ChildPath "checkpoint_$ck"
$env:EVOLUTION_RESUMED_FROM = $checkpoint

Write-Host "resuming $Run from checkpoint_$ck (task=$Task, iterations=$Iterations)"
Write-Host "run_id=$($env:EVOLUTION_RUN_ID)"
& $py -m control_plane.runner.entrypoint $prog $eval `
    --config "configs/oe_max/evolution.yaml" `
    --checkpoint $checkpoint `
    --iterations $Iterations --output $Run
exit $LASTEXITCODE
