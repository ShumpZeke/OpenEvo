# Evolution - full test suite.
#
# Runs the preserved upstream tests first: a change that breaks them is a
# regression in the fork, not merely a control-plane bug.
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot "scripts/_common.ps1")
$py = Get-VenvPythonOrExit -Root $PSScriptRoot
$rc = 0

Write-Host "=== upstream OpenEvolve compatibility suite ==="
& $py -m pytest tests/ -q -m "not slow" --ignore=tests/evolution --ignore=tests/oe_max
if ($LASTEXITCODE -ne 0) { $rc = 1 }

Write-Host "`n=== control plane ==="
& $py -m pytest tests/evolution -q
if ($LASTEXITCODE -ne 0) { $rc = 1 }

Write-Host "`n=== OE-MAX (broker, limiter, gates, search) ==="
& $py -m pytest tests/oe_max -q
if ($LASTEXITCODE -ne 0) { $rc = 1 }

if (Test-Path "web/node_modules") {
  Write-Host "`n=== web typecheck ==="
  Push-Location web
  try { npm run typecheck; if ($LASTEXITCODE -ne 0) { $rc = 1 } } finally { Pop-Location }
}

Write-Host ""
if ($rc -eq 0) { Write-Host "All suites passed." } else { Write-Host "Failures reported above." }
exit $rc
