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
$py = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run .\bootstrap.ps1 first."; exit 1 }

$cfg = switch ($Profile) {
  "max"   { "configs\oe_max\evolution.yaml" }
  "stock" { "configs\oe_max\stock_baseline.yaml" }
  default { $Profile }
}
$prog = "examples\$Task\initial_program.py"
$eval = "examples\$Task\evaluator.py"
foreach ($f in @($prog, $eval, $cfg)) {
  if (-not (Test-Path $f)) { Write-Error "missing: $f"; exit 1 }
}
if (-not $Output) {
  $Output = "runs\$(Get-Date -Format 'yyyyMMdd-HHmmss')-$Task-$Profile"
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null

$port = if ($env:OE_MAX_PORT) { $env:OE_MAX_PORT } else { "8787" }
if ($Profile -eq "max") {
  try { Invoke-WebRequest "http://127.0.0.1:$port/health" -TimeoutSec 5 -UseBasicParsing | Out-Null }
  catch { Write-Error "The broker is not running. Start it: .\scripts\start-broker.ps1"; exit 1 }
}
Write-Host "task=$Task profile=$Profile iterations=$Iterations"
Write-Host "output=$Output"
& $py openevolve-run.py $prog $eval --config $cfg --iterations $Iterations --output $Output
