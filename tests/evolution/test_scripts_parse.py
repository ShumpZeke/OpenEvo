"""Every operator script must at least parse.

`scripts/run-evolution.ps1` and `scripts/start-broker.ps1` are the primary way a
person starts this project, and nothing else in the suite reads them. A stray
brace or a mangled here-string in either is invisible until someone tries to run
it — which is exactly how the broker's startup crash was found, one layer up.

This does not run anything. It asks each shell to parse its own scripts, which
is cheap, safe, and catches the whole class of "I edited a script and broke it".
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", "node_modules", ".git", "build", "dist"}


def _scripts(suffix):
    for path in sorted(ROOT.rglob(f"*{suffix}")):
        if not any(part in SKIP_DIRS for part in path.parts):
            yield path


POWERSHELL_SCRIPTS = list(_scripts(".ps1"))
SHELL_SCRIPTS = list(_scripts(".sh"))


def test_the_repository_still_has_operator_scripts():
    """Guards the two parametrised tests below from silently covering nothing."""
    assert POWERSHELL_SCRIPTS, "no .ps1 files found"
    assert SHELL_SCRIPTS, "no .sh files found"
    names = {p.name for p in POWERSHELL_SCRIPTS} | {p.name for p in SHELL_SCRIPTS}
    # The two that matter most, named so a rename has to be deliberate.
    assert "run-evolution.ps1" in names
    assert "start-broker.ps1" in names


@pytest.mark.skipif(sys.platform != "win32", reason="needs PowerShell's own parser")
@pytest.mark.parametrize(
    "script", POWERSHELL_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT))
)
def test_powershell_scripts_parse(script):
    command = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script}', "
        "[ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { "
        "  $errors | ForEach-Object { "
        "    Write-Output ('line ' + $_.Extent.StartLineNumber + ': ' + $_.Message) }; "
        "  exit 1 } "
        "else { exit 0 }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, errors="replace", timeout=180,
    )
    assert completed.returncode == 0, (
        f"{script.relative_to(ROOT)} does not parse:\n{completed.stdout}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
@pytest.mark.parametrize(
    "script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT))
)
def test_shell_scripts_parse(script):
    """`bash -n` reads the script and checks syntax without executing it."""
    completed = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True, text=True, errors="replace", timeout=180,
    )
    assert completed.returncode == 0, (
        f"{script.relative_to(ROOT)} does not parse:\n{completed.stderr}"
    )
