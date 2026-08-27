# Project memory: where you left off, and what you noted.
#
#   .\scripts\memory.ps1                          # the digest
#   .\scripts\memory.ps1 note "tried hy3 as reasoner" --kind decision
#   .\scripts\memory.ps1 search hy3
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_common.ps1")
$py = Get-VenvPythonOrExit -Root $PWD
& $py -m control_plane.memory.cli @args
exit $LASTEXITCODE
