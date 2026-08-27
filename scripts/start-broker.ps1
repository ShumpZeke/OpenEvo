# Terminal 1: the OE-MAX provider broker.
# OpenEvolve points at this; upstream credentials never leave this process.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD

Import-DotEnv -Path (Join-Path $PWD ".env")

$port = if ($env:OE_MAX_PORT) { $env:OE_MAX_PORT } else { "8787" }
& $py -m oe_max.broker.cli --port $port @args
exit $LASTEXITCODE
