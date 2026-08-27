# Run the same task with each optional behaviour on and off, and report whether
# it helped. Requires the control plane (.\run.ps1).
#
#   .\scripts\ablation.ps1 --arms operators,operator_bandit --repeats 2
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD
& $py (Join-Path $PWD "scripts/ablation.py") @args
exit $LASTEXITCODE
