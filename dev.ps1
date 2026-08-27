# Evolution - development servers (API with reload + Vite dev server).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot "scripts/_common.ps1")
$py = Get-VenvPythonOrExit -Root $PSScriptRoot

$port = if ($env:EVOLUTION_PORT) { $env:EVOLUTION_PORT } else { "8000" }
$api = Start-Process -PassThru -NoNewWindow -FilePath $py -ArgumentList @(
  "-m", "uvicorn", "control_plane.api.app:create_app",
  "--factory", "--host", "127.0.0.1", "--port", $port, "--reload"
)
try {
  if (Test-Path "web/node_modules") {
    Push-Location web
    try { npm run dev } finally { Pop-Location }
  } else {
    Write-Warning "web/node_modules missing - run .\bootstrap.ps1"
    Wait-Process -Id $api.Id
  }
} finally {
  Stop-Process -Id $api.Id -ErrorAction SilentlyContinue
}
