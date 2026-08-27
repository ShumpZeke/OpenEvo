# Live terminal dashboard: providers, rate windows, routes, roles, evolution.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD
& $py -m oe_max.dashboard @args
exit $LASTEXITCODE
