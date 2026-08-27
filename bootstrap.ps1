# Evolution - reproducible bootstrap (Windows).
# Mirrors bootstrap.sh. Nothing is installed globally.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot "scripts/_common.ps1")
$venv = Join-Path $PSScriptRoot ".venv"
$fail = 0

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [x]  $m" -ForegroundColor Red; $script:fail = 1 }

Say "Detecting platform"
Ok "$([System.Environment]::OSVersion.VersionString) $($env:PROCESSOR_ARCHITECTURE)"

Say "Checking required tools"
foreach ($t in @("git", "python")) {
  if (Get-Command $t -ErrorAction SilentlyContinue) { Ok "$t - $(& $t --version 2>&1 | Select-Object -First 1)" }
  else { Bad "$t not found" }
}
if (Get-Command node -ErrorAction SilentlyContinue) { Ok "node - $(node --version)" }
else { Warn "node not found - the web UI cannot be built (the API still runs)" }

Say "Creating Python environment"
if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv $venv | Out-Null }
else { python -m venv $venv }

# Resolved rather than assumed. Windows puts the interpreter in
# Scripts\python.exe and every other platform in bin/python, and hardcoding
# the first is what made this script unrunnable - and therefore unchecked -
# anywhere but Windows.
$py = Get-VenvPython -Root $PSScriptRoot
if (-not $py) { Bad "virtualenv creation produced no interpreter under $venv" }
Ok $venv

Say "Installing the engine and control plane"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install -e . | Out-Null
Ok "openevolve (engine) + control plane installed"

Say "Initialising control-plane storage"
New-Item -ItemType Directory -Force -Path (Join-Path ".evolution" "workspace") | Out-Null
& $py -c @"
import sys, os
sys.path.insert(0, os.getcwd())
from control_plane.storage.store import Store
s = Store(os.path.join('.evolution','workspace','control_plane.db'))
print('  schema ready:', s.path); s.close()
"@
Ok "storage initialised"

Say "Checking the agent sandbox (OpenCode isolation)"
& $py -c @"
import sys, os
sys.path.insert(0, os.getcwd())
from control_plane.sandbox.opencode import OpenCodeIsolation
r = OpenCodeIsolation(os.path.join(os.getcwd(), '.evolution', 'workspace')).preflight()
print('  isolation level :', r.level.value)
print('  opencode binary :', r.binary or 'not found')
print('  container       :', 'docker' if r.docker_available else 'none')
print('  oh-my-openagent :', r.omo.get('binary') or 'not installed (optional)')
for w in r.warnings: print('  note:', w)
for x in r.reasons:  print('  reason:', x)
"@

Say "Building the web Control Center"
if (Get-Command npm -ErrorAction SilentlyContinue) {
  Push-Location web
  try { npm install --no-audit --no-fund | Out-Null; npm run build | Out-Null; Ok "web/dist built" }
  catch { Warn "web build failed - the API will serve without a UI" }
  finally { Pop-Location }
} else { Warn "npm unavailable - skipping the web build" }

Say "Creating .env from the example (no secrets are written)"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Ok ".env created" }

Say "Running the smoke test"
& $py -m pytest tests/evolution -q
if ($LASTEXITCODE -eq 0) { Ok "control-plane tests passed" } else { Warn "see .\test.ps1 for detail" }

Write-Host ""
if ($fail -eq 0) { Say "Bootstrap complete" } else { Say "Bootstrap finished with missing prerequisites" }
Write-Host @"

  Start everything      .\run.ps1
  Control Center        http://127.0.0.1:8000
  Classic visualizer    .\run.ps1 classic <checkpoint_dir>   -> http://127.0.0.1:8080
  Original CLI          .\run.ps1 cli --help
  Tests                 .\test.ps1
  Dev servers           .\dev.ps1

"@
exit $fail
