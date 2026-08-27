"""
The launch scripts must install telemetry.

This guards a bug that produced no error and no warning. `run-evolution.sh`
exec'd the plain upstream CLI, which installs no instrumentation — and every
OE_MAX_* feature is installed BY that instrumentation. So

    OE_MAX_OPERATORS=1 ./scripts/run-evolution.sh --iterations 12

set an environment variable that nothing read. The run succeeded, the score
improved, and no operator steering, attribution, multi-offspring, verification
or bandit happened at all. Several documented "verified on a N-iteration run"
commands could not have done what they claimed on that path.

A flag that silently does nothing is worse than one that errors, so this is
pinned rather than left to be rediscovered.
"""

from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every script that starts the engine. `resume-evolution` had the identical
# bug and was fixed later, so it belongs here rather than in a test of its own.
LAUNCHERS = [
    os.path.join(ROOT, "scripts", "run-evolution.sh"),
    os.path.join(ROOT, "scripts", "run-evolution.ps1"),
    os.path.join(ROOT, "scripts", "resume-evolution.sh"),
    os.path.join(ROOT, "scripts", "resume-evolution.ps1"),
]


@pytest.mark.parametrize("path", LAUNCHERS, ids=lambda p: os.path.basename(p))
def test_the_launcher_runs_the_instrumented_entrypoint(path):
    body = open(path, encoding="utf-8").read()

    assert "control_plane.runner.entrypoint" in body, (
        f"{os.path.basename(path)} does not use the instrumented entrypoint; "
        f"every OE_MAX_* flag would be silently ignored")


def _executable_lines(path: str) -> str:
    """
    The script with comments stripped.

    Both launchers *mention* `openevolve-run.py` in a comment explaining why
    they no longer call it. That explanation is worth keeping, so the check has
    to look at what the script runs rather than at what it says.
    """
    out = []
    for line in open(path, encoding="utf-8").read().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize("path", LAUNCHERS, ids=lambda p: os.path.basename(p))
def test_the_launcher_does_not_call_the_bare_upstream_cli(path):
    assert "openevolve-run.py" not in _executable_lines(path), (
        f"{os.path.basename(path)} still invokes the uninstrumented CLI")


@pytest.mark.parametrize("path", LAUNCHERS, ids=lambda p: os.path.basename(p))
def test_the_launcher_supplies_a_run_id(path):
    """
    The entrypoint exits 2 without one, so a launcher that omits it turns a
    silent no-op into a hard failure — better, but still broken.
    """
    body = open(path, encoding="utf-8").read()

    assert "EVOLUTION_RUN_ID" in body


@pytest.mark.parametrize("path", LAUNCHERS, ids=lambda p: os.path.basename(p))
def test_the_launcher_lets_the_caller_override_the_run_id(path):
    """
    The ablation and route-experiment harnesses pool runs by id. A launcher
    that always generated its own would make an arm's runs unpoolable.
    """
    body = open(path, encoding="utf-8").read()

    if path.endswith(".sh"):
        assert '${EVOLUTION_RUN_ID:-' in body, "run id is not caller-overridable"
    else:
        assert "if (-not $env:EVOLUTION_RUN_ID)" in body
