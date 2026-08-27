# Run the same task on each of several routes and compare their mutation
# quality. Requires the broker (.\scripts\start-broker.ps1) and the control
# plane (.\run.ps1) to be up.
#
#   .\scripts\route-experiment.ps1 --routes nemotron-3-ultra-free,hy3-free --repeats 3
#
# Name routes that are alive: `.\scripts\verify-providers.ps1` says which are.
# An arm pinned to a withdrawn model is a waiting game with no result at the
# end - which is exactly how two attempts at this were lost to `x-preview-f-free`
# before it was removed from OpenCode Zen altogether.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD
& $py (Join-Path $PWD "scripts/route_experiment.py") @args
exit $LASTEXITCODE
