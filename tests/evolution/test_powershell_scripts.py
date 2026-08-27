"""
The PowerShell surface must at least parse.

These scripts shipped having never been executed — there was no PowerShell in
the development container — and a script nobody can run is a script nobody has
checked. Running them found three real bugs, including one where
`.\\run.ps1 cli` passed "cli" to the CLI as an argument because PowerShell's
range operator counts down when the end is below the start.

A parse check is the cheap half of preventing that recurring: it catches every
syntax error and costs nothing. It is skipped when `pwsh` is absent, which is
honest — the alternative is a test that silently passes by not running, and
the skip says so out loud.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PWSH = shutil.which("pwsh") or shutil.which("powershell")
requires_pwsh = pytest.mark.skipif(
    PWSH is None, reason="no PowerShell on PATH; cannot parse-check .ps1")


def _scripts():
    found = glob.glob(os.path.join(ROOT, "*.ps1"))
    found += glob.glob(os.path.join(ROOT, "scripts", "*.ps1"))
    return sorted(found)


def test_the_powershell_surface_exists():
    """
    Guards against the .sh side gaining an entry point the .ps1 side does not.
    A Windows-first project that quietly drops a Windows script is worse off
    than one that never had it, because the docs keep promising it.
    """
    names = {os.path.basename(p)[:-4] for p in _scripts()}

    for expected in ("bootstrap", "run", "test", "dev", "start-broker",
                     "run-evolution", "resume-evolution", "dashboard",
                     "verify-providers", "ablation", "route-experiment",
                     "memory"):
        assert expected in names, f"no {expected}.ps1 to match the shell script"


@requires_pwsh
@pytest.mark.parametrize(
    "path", _scripts(), ids=lambda p: os.path.basename(p))
def test_the_script_parses(path):
    check = (
        "$t=$null;$e=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count){$e|ForEach-Object{Write-Output "
        "(\"line \" + $_.Extent.StartLineNumber + ': ' + $_.Message)};exit 1}"
    )
    proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-Command", check],
                          capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, f"{os.path.basename(path)}:\n{proc.stdout}"


@requires_pwsh
def test_every_script_that_needs_the_venv_uses_the_shared_resolver():
    """
    The hardcoded `.venv\\Scripts\\python.exe` is what made the whole surface
    untestable off Windows. One resolver handles both layouts; a script that
    reintroduces the literal path silently breaks WSL, macOS and CI again.
    """
    offenders = []
    for path in _scripts():
        body = open(path, encoding="utf-8").read()
        executable = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#"))
        if "Scripts\\python.exe" in executable or "Scripts/python.exe" in executable:
            if "_common.ps1" not in body:
                offenders.append(os.path.basename(path))

    assert offenders == [], f"hardcoded venv path in: {offenders}"
