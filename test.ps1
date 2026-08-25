# Evolution - full test suite.
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run .\bootstrap.ps1 first."; exit 1 }
$rc = 0

Write-Host "=== upstream OpenEvolve compatibility suite ==="
& $py -m pytest tests\ -q -m "not slow" --ignore=tests\evolution
if ($LASTEXITCODE -ne 0) { $rc = 1 }

Write-Host "`n=== control plane ==="
& $py -m pytest tests\evolution -q
if ($LASTEXITCODE -ne 0) { $rc = 1 }

if (Test-Path "web\node_modules") {
  Write-Host "`n=== web typecheck ==="
  Push-Location web; npm run typecheck; if ($LASTEXITCODE -ne 0) { $rc = 1 }; Pop-Location
}

Write-Host ""
if ($rc -eq 0) { Write-Host "All suites passed." } else { Write-Host "Failures reported above." }
exit $rc
