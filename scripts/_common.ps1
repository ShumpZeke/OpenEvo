# Shared helpers for the PowerShell entry points.
#
# Dot-source with:  . (Join-Path $PSScriptRoot "_common.ps1")
#
# WHY THIS EXISTS. Every script hardcoded `.venv\Scripts\python.exe`, which is
# correct on Windows and resolves to nothing anywhere else. That made the whole
# PowerShell surface untestable outside Windows — and it had, in fact, never
# been run. Scripts nobody can execute are scripts nobody has checked.
#
# PowerShell Core runs on Linux and macOS too, so resolving both layouts costs
# two lines and buys three things: the scripts work under WSL and on a Mac,
# they can be exercised in CI on a Linux runner, and a Windows user gets
# exactly the behaviour they had before.
#
# Forward slashes are used deliberately. .NET accepts them on Windows, so one
# spelling works everywhere and there is no separator branching to get wrong.

Set-StrictMode -Version Latest

function Get-VenvPython {
    <#
    .SYNOPSIS
    The project's virtualenv interpreter, or $null when there is not one.

    .DESCRIPTION
    Returns $null rather than throwing so each caller can print its own
    instruction — "run bootstrap" is the right advice from `test.ps1` and the
    wrong advice from inside `bootstrap.ps1` itself.
    #>
    param([Parameter(Mandatory)][string]$Root)

    foreach ($rel in @(".venv/Scripts/python.exe", ".venv/bin/python")) {
        $candidate = Join-Path $Root $rel
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Get-VenvPythonOrExit {
    <#
    .SYNOPSIS
    The interpreter, or exit 1 with the bootstrap instruction.
    #>
    param([Parameter(Mandatory)][string]$Root)

    $py = Get-VenvPython -Root $Root
    if (-not $py) {
        Write-Error "No environment found at $Root/.venv - run .\bootstrap.ps1 first."
        exit 1
    }
    return $py
}

function Get-VenvScript {
    <#
    .SYNOPSIS
    A console script installed in the venv (openevolve-run, evolution-server).

    .DESCRIPTION
    Windows installs these as `.exe` shims under Scripts\; every other platform
    puts an extension-less executable in bin/. Returns $null when absent.
    #>
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Name
    )

    foreach ($rel in @("Scripts/$Name.exe", "bin/$Name")) {
        $candidate = Join-Path $Root ".venv" | Join-Path -ChildPath $rel
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Import-DotEnv {
    <#
    .SYNOPSIS
    Load KEY=VALUE lines from a .env file into the process environment.

    .DESCRIPTION
    Only variable NAMES are ever recorded in telemetry; values are never
    logged. Blank lines, comments and lines without '=' are skipped, and
    surrounding quotes are stripped so KEY="value" behaves as a shell would.

    Existing environment variables win. An operator who exported a key for one
    command should not have it silently overridden by a stale .env.
    #>
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or ($trimmed -notmatch "=")) { continue }
        $key, $value = $trimmed -split "=", 2
        $key = $key.Trim()
        if (-not $key) { continue }
        if ([Environment]::GetEnvironmentVariable($key, "Process")) { continue }
        $value = $value.Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}
