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
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# `examples/` and `openevolve/` are upstream and byte-identical. Their scripts
# are not ours to keep parsing, and a failure there would be a finding about
# upstream rather than about this fork.
SKIP_DIRS = {
    ".venv", "node_modules", ".git", "build", "dist",
    "examples", "openevolve", "upstream",
}


def _scripts(suffix):
    for path in sorted(ROOT.rglob(f"*{suffix}")):
        if not any(part in SKIP_DIRS for part in path.parts):
            yield path


def _bash_can_parse():
    """Whether the `bash` on PATH actually works, not merely whether it exists.

    On a GitHub Windows runner `bash` resolves to the WSL launcher, which is on
    PATH with no distribution behind it and fails on everything -- so
    `shutil.which("bash")` said yes and every script "did not parse", with an
    empty stderr to explain it. Prove the tool works on a script known to be
    valid before trusting its verdict on ours.
    """
    if shutil.which("bash") is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")
        try:
            return subprocess.run(
                ["bash", "-n", str(probe)], capture_output=True, timeout=60
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


BASH_WORKS = _bash_can_parse()


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


@pytest.mark.skipif(not BASH_WORKS, reason="no working bash on PATH")
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
