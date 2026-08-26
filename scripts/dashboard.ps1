# Live terminal dashboard.
Set-Location (Join-Path $PSScriptRoot "..")
& (Join-Path $PWD ".venv\Scripts\python.exe") -m oe_max.dashboard @args
