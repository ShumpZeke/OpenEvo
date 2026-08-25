# Evolution - development servers (API with reload + Vite dev server).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run .\bootstrap.ps1 first."; exit 1 }
$port = if ($env:EVOLUTION_PORT) { $env:EVOLUTION_PORT } else { "8000" }
$api = Start-Process -PassThru -NoNewWindow $py @("-m","uvicorn","control_plane.api.app:create_app","--factory","--host","127.0.0.1","--port",$port,"--reload")
try {
  if (Test-Path "web\node_modules") { Push-Location web; npm run dev; Pop-Location }
  else { Write-Warning "web\node_modules missing - run .\bootstrap.ps1"; Wait-Process -Id $api.Id }
} finally { Stop-Process -Id $api.Id -ErrorAction SilentlyContinue }
