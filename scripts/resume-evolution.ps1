# Resume from the newest checkpoint of a run directory.
param([Parameter(Mandatory=$true)][string]$Run, [int]$Iterations = 20)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = Join-Path $PWD ".venv\Scripts\python.exe"
$ck = Get-ChildItem "$Run\checkpoints\checkpoint_*" -Directory -ErrorAction SilentlyContinue |
      ForEach-Object { [int]($_.Name -replace 'checkpoint_','') } | Sort-Object | Select-Object -Last 1
if (-not $ck) { Write-Error "no checkpoint found in $Run"; exit 1 }
Write-Host "resuming $Run from checkpoint_$ck"
& $py openevolve-run.py examples\function_minimization\initial_program.py `
    examples\function_minimization\evaluator.py `
    --config configs\oe_max\evolution.yaml `
    --checkpoint "$Run\checkpoints\checkpoint_$ck" `
    --iterations $Iterations --output $Run
