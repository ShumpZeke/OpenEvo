# Evolution - launch the control plane, or the classic visualizer.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot "scripts/_common.ps1")
$py = Get-VenvPythonOrExit -Root $PSScriptRoot

$mode = if ($args.Count -gt 0) { $args[0] } else { "server" }

# Everything after the mode. Written as a guarded slice because PowerShell's
# range operator counts DOWN when the end is below the start: with one
# argument, `$args[1..($args.Count-1)]` is `$args[1..0]`, which yields
# $args[1] and $args[0] — so `.\run.ps1 cli` passed "cli" to the CLI as an
# argument. Verified, not theorised.
#
# Assigned in two statements rather than as `... else { @() }`, because an
# empty array emitted from an `if` unrolls to nothing and leaves $rest as
# $null. That reads the same until Set-StrictMode is on -- which _common.ps1
# turns on -- and then `$rest.Count` throws PropertyNotFoundStrict and takes
# `.\run.ps1 provider`, `classic` and `cli` down with it. @() around the slice
# keeps a one-element result an array too, so $rest[0] means the first
# argument rather than the first character of it.
$rest = @()
if ($args.Count -gt 1) { $rest = @($args[1..($args.Count - 1)]) }

switch ($mode) {
  "classic" {
    if ($rest.Count -lt 1) { Write-Error "usage: .\run.ps1 classic <checkpoint_dir>"; exit 2 }
    $port = if ($env:EVOLUTION_CLASSIC_PORT) { $env:EVOLUTION_CLASSIC_PORT } else { "8080" }
    & $py (Join-Path $PSScriptRoot "scripts/visualizer.py") --path $rest[0] --port $port
  }
  "provider" {
    $port = if ($rest.Count -ge 1) { $rest[0] } else { "8765" }
    & $py (Join-Path $PSScriptRoot "scripts/local_provider.py") --port $port
  }
  "cli" {
    $cli = Get-VenvScript -Root $PSScriptRoot -Name "openevolve-run"
    if (-not $cli) { Write-Error "openevolve-run is not installed - run .\bootstrap.ps1"; exit 1 }
    & $cli @rest
  }
  default {
    $port  = if ($env:EVOLUTION_PORT) { $env:EVOLUTION_PORT } else { "8000" }
    $vhost = if ($env:EVOLUTION_HOST) { $env:EVOLUTION_HOST } else { "127.0.0.1" }
    Write-Host "Evolution Control Center -> http://${vhost}:$port"
    & $py -m uvicorn "control_plane.api.app:create_app" --factory --host $vhost --port $port
  }
}
exit $LASTEXITCODE
