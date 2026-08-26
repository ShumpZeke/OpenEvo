# Terminal 1: the OE-MAX provider broker.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run .\bootstrap.ps1 first."; exit 1 }
if (Test-Path ".env") {
  Get-Content ".env" | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
    $k, $v = $_ -split "=", 2
    [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
  }
}
$port = if ($env:OE_MAX_PORT) { $env:OE_MAX_PORT } else { "8787" }
& $py -m oe_max.broker.cli --port $port @args
