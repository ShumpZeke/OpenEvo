# Check a fully-local setup end to end, without running an evolution.
#
#   .\scripts\local\check-local.ps1
#
# Reports what is true rather than what should be true. Every line is a probe,
# not a declaration -- if something says OK here, it was measured a moment ago.
# Exit code is non-zero when something that must work does not.

[CmdletBinding()]
param(
    [int]$BrokerPort = 8787,
    [int]$ControlPort = 8000
)

$script:fail = 0
$script:warn = 0
function Ok   ($m) { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow; $script:warn++ }
function Bad  ($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red;    $script:fail++ }
function Head ($m) { Write-Host "`n$m" -ForegroundColor Cyan }

function Try-Json($url, $timeout = 5) {
    try { Invoke-RestMethod -Uri $url -TimeoutSec $timeout } catch { $null }
}

Head "Hardware"
$cs  = Get-CimInstance Win32_ComputerSystem
$os  = Get-CimInstance Win32_OperatingSystem
$ramTotal = $cs.TotalPhysicalMemory / 1GB
$ramFree  = $os.FreePhysicalMemory * 1KB / 1GB
Ok ("RAM {0:N1} GB total, {1:N1} GB free" -f $ramTotal, $ramFree)
if ($ramFree -lt 2) {
    Warn "under 2 GB free -- a large model will page, and paging looks like the model being slow"
}
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = (nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader) -join ""
    Ok "GPU $gpu"
} else {
    Warn "no nvidia-smi; CPU-only inference will be very slow"
}

Head "Runtimes"
$haveOllama = [bool](Get-Command ollama -ErrorAction SilentlyContinue)
if ($haveOllama) { Ok "ollama      $((ollama --version) -replace 'ollama version is ','')" }
else { Warn "ollama not on PATH" }
foreach ($n in @("llama-server", "lms")) {
    if (Get-Command $n -ErrorAction SilentlyContinue) { Ok "$n present" }
    else { Warn "$n not on PATH (optional)" }
}

Head "Ollama tuning"
# These are what let a model larger than VRAM run at a usable speed. Absent,
# nothing is broken -- it is just measurably slower, so they are warnings.
$expected = @{
    OLLAMA_FLASH_ATTENTION   = "1"
    OLLAMA_KV_CACHE_TYPE     = "q8_0"
    OLLAMA_MAX_LOADED_MODELS = "1"
    OLLAMA_NUM_PARALLEL      = "1"
}
foreach ($k in $expected.Keys | Sort-Object) {
    $v = [Environment]::GetEnvironmentVariable($k, "User")
    if ($v -eq $expected[$k]) { Ok "$k = $v" }
    else { Warn "$k = '$v' (expected '$($expected[$k])') -- see docs/local-tuning.md" }
}

Head "Local endpoints"
$endpoints = @(
    @{ name = "ollama";   url = "http://127.0.0.1:11434/v1/models" },
    @{ name = "lmstudio"; url = "http://127.0.0.1:1234/v1/models" },
    @{ name = "vllm";     url = "http://127.0.0.1:8000/v1/models" },
    @{ name = "llamacpp"; url = "http://127.0.0.1:8080/v1/models" }
)
$serving = 0
foreach ($e in $endpoints) {
    $r = Try-Json $e.url 3
    if ($r -and $r.data) {
        $serving++
        Ok ("{0,-9} {1} model(s): {2}" -f $e.name, $r.data.Count,
            (($r.data | Select-Object -First 3 | ForEach-Object { $_.id }) -join ", "))
    } else {
        Write-Host ("  [--]   {0,-9} not running" -f $e.name) -ForegroundColor DarkGray
    }
}
if ($serving -eq 0) { Bad "no local endpoint is serving -- start one, e.g. 'ollama serve'" }

Head "Resident model"
$ps = Try-Json "http://127.0.0.1:11434/api/ps" 5
if ($ps -and $ps.models) {
    foreach ($m in $ps.models) {
        $pct = if ($m.size) { 100 * $m.size_vram / $m.size } else { 0 }
        $line = "{0}  {1:N2} GB, {2:N0}% in VRAM, ctx {3}" -f $m.name, ($m.size/1e9), $pct, $m.context_length
        if ($pct -lt 25) { Warn "$line -- mostly on the CPU; prompt evaluation will dominate" }
        else { Ok $line }
    }
} else {
    Write-Host "  [--]   nothing resident (it loads on first request)" -ForegroundColor DarkGray
}

Head "Broker (OE_MAX_LOCAL_ONLY)"
$health = Try-Json "http://127.0.0.1:$BrokerPort/health" 5
if (-not $health) {
    Warn "broker not running on :$BrokerPort -- start it with OE_MAX_LOCAL_ONLY=1"
} else {
    $providers = @($health.providers.PSObject.Properties.Name)
    $cloud = $providers | Where-Object { $_ -notin @("ollama","lmstudio","vllm","llamacpp") }
    if ($cloud) { Bad "commercial providers present in local-only mode: $($cloud -join ', ')" }
    else { Ok "only local providers: $($providers -join ', ')" }

    if ($health.routes) {
        Ok "routes: $($health.routes -join ', ')"
        $offbox = $health.routes | Where-Object { $_ -notmatch '^(ollama|lmstudio|vllm|llamacpp)/' }
        if ($offbox) { Bad "route not on this machine: $($offbox -join ', ')" }
    } else {
        Warn "broker has no routes -- no local server was up when it started; restart it"
    }
}

Head "Control Center"
$cp = Try-Json "http://127.0.0.1:$ControlPort/api/health" 5
if (-not $cp) {
    Warn "control plane not running on :$ControlPort (.\run.ps1)"
} else {
    Ok "api healthy, workspace $($cp.workspace)"
    foreach ($p in @("/api/providers","/api/control/capabilities","/api/query/runs",
                     "/api/scientific/capabilities")) {
        try {
            $code = (Invoke-WebRequest "http://127.0.0.1:$ControlPort$p" -TimeoutSec 5 -UseBasicParsing).StatusCode
            if ($code -eq 200) { Ok "$p -> 200" } else { Bad "$p -> $code" }
        } catch { Bad "$p -> $($_.Exception.Message)" }
    }
}

Head "Result"
if ($script:fail -gt 0) {
    Write-Host "$($script:fail) failure(s), $($script:warn) warning(s)" -ForegroundColor Red
    exit 1
}
if ($script:warn -gt 0) {
    Write-Host "no failures, $($script:warn) warning(s) -- usable, not optimal" -ForegroundColor Yellow
    exit 0
}
Write-Host "everything checked is working" -ForegroundColor Green
exit 0
