# Evolution - launch the control plane, or the classic visualizer.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "No environment found. Run .\bootstrap.ps1 first."; exit 1 }

$mode = if ($args.Count -gt 0) { $args[0] } else { "server" }
switch ($mode) {
  "classic" {
    if ($args.Count -lt 2) { Write-Error "usage: .\run.ps1 classic <checkpoint_dir>"; exit 2 }
    $port = if ($env:EVOLUTION_CLASSIC_PORT) { $env:EVOLUTION_CLASSIC_PORT } else { "8080" }
    & $py scripts\visualizer.py --path $args[1] --port $port
  }
  "provider" {
    $port = if ($args.Count -gt 1) { $args[1] } else { "8765" }
    & $py scripts\local_provider.py --port $port
  }
  "cli" { & (Join-Path $PSScriptRoot ".venv\Scripts\openevolve-run.exe") @($args[1..($args.Count-1)]) }
  default {
    $port = if ($env:EVOLUTION_PORT) { $env:EVOLUTION_PORT } else { "8000" }
    $vhost = if ($env:EVOLUTION_HOST) { $env:EVOLUTION_HOST } else { "127.0.0.1" }
    Write-Host "Evolution Control Center -> http://127.0.0.1:$port"
    & $py -m uvicorn "control_plane.api.app:create_app" --factory --host $vhost --port $port
  }
}
