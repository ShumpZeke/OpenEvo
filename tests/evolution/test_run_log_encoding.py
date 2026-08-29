"""A run log must not silently discard its most important lines.

Upstream marks a new best with a star and writes score changes with an arrow.
On Windows the child inherits the console code page -- cp1252 here -- `logging`
cannot encode those characters, the handler raises, and the **whole record is
dropped**. Not mangled into question marks: gone. Measured on this box, a log
line containing an emoji arrived as 0 bytes without `PYTHONIOENCODING` and 48
bytes with it.

So the lines a run log exists to carry were the ones being lost, and a run that
found a new best could produce a log that never says so.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# A star and an arrow, the two characters upstream actually uses.
LOG_LINE = "\U0001f31f New best at iteration 6: 1.4061 → 1.4987"
CHILD = (
    "import logging, sys;"
    "logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(message)s');"
    f"logging.info({LOG_LINE!r})"
)


def _run_child(tmp_path, env_extra):
    """Run the child with stdout to a binary file, as the runner does."""
    env = os.environ.copy()
    env.update(env_extra)
    log_path = tmp_path / "run.log"
    with open(log_path, "wb") as out:
        subprocess.run([sys.executable, "-c", CHILD], stdout=out,
                       stderr=subprocess.DEVNULL, env=env, timeout=120)
    return log_path.read_text(encoding="utf-8", errors="replace")


def test_utf8_child_keeps_the_line(tmp_path):
    """With the setting the runner applies, the record survives intact."""
    body = _run_child(tmp_path, {"PYTHONIOENCODING": "utf-8"})
    assert "New best at iteration 6" in body
    assert "1.4061" in body and "1.4987" in body


@pytest.mark.skipif(os.name != "nt", reason="the loss is a Windows code-page behaviour")
def test_a_legacy_code_page_loses_the_line_entirely(tmp_path):
    """The failure being guarded against, demonstrated rather than asserted.

    If this ever starts passing -- Python defaulting to UTF-8 on Windows, say --
    the guard is no longer load-bearing and can be reconsidered. Until then it
    is the reason the runner sets the variable.
    """
    body = _run_child(tmp_path, {"PYTHONIOENCODING": "cp1252"})
    assert "New best at iteration 6" not in body, (
        "cp1252 no longer drops the record; the PYTHONIOENCODING guard may be "
        "obsolete"
    )


def test_the_runner_sets_it_for_every_run():
    """The manager builds the child environment; this is where it has to land."""
    source = (ROOT / "control_plane" / "runner" / "manager.py").read_text(
        encoding="utf-8")
    assert '"PYTHONIOENCODING": "utf-8"' in source


@pytest.mark.parametrize("script,expected", [
    ("scripts/run-evolution.ps1", '$env:PYTHONIOENCODING = "utf-8"'),
    ("scripts/run-evolution.sh", 'export PYTHONIOENCODING='),
])
def test_the_run_scripts_set_it_too(script, expected):
    """Both launch paths need it -- a run started from the shell writes the same
    log the Control Center reads."""
    assert expected in (ROOT / script).read_text(encoding="utf-8")


def test_the_reader_side_agrees_on_utf8():
    """The child now writes UTF-8; the manager must not read it as something
    else, or the fix trades lost lines for mojibake."""
    source = (ROOT / "control_plane" / "runner" / "manager.py").read_text(
        encoding="utf-8")
    assert 'encoding="utf-8"' in source


def test_the_entrypoint_covers_the_paths_the_run_manager_does_not(tmp_path):
    """`scripts/resume-evolution.*` and a direct `-m` invocation start the same
    engine, and neither goes through the run manager that sets the variable.

    Both halves are checked because they cover different processes: the
    environment variable is what a spawned worker inherits, and reconfiguring
    this interpreter's streams is what fixes the process already running.
    """
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys, os, logging; sys.path.insert(0, '.');"
         "import control_plane.runner.entrypoint;"
         "logging.basicConfig(stream=sys.stdout, level=logging.INFO,"
         " format='%(message)s');"
         f"logging.info({LOG_LINE!r});"
         "print('CHILDREN_GET=' + os.environ.get('PYTHONIOENCODING', 'unset'))"],
        capture_output=True, text=True, errors="replace",
        env={**os.environ, "PYTHONIOENCODING": ""},
        cwd=str(ROOT), timeout=180,
    )
    assert child.returncode == 0, child.stderr
    assert "UnicodeEncodeError" not in child.stderr, child.stderr
    # This interpreter's own record survived ...
    assert "New best at iteration 6" in child.stdout
    # ... and a worker spawned from it would inherit a usable encoding.
    assert "CHILDREN_GET=utf-8" in child.stdout


def test_an_explicit_encoding_choice_is_not_overridden():
    """Someone who set PYTHONIOENCODING on purpose gets what they asked for.

    The failure being guarded against is the variable being absent, not a value
    somebody chose.
    """
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys, os; sys.path.insert(0, '.');"
         "import control_plane.runner.entrypoint;"
         "print('KEPT=' + os.environ.get('PYTHONIOENCODING', 'unset'))"],
        capture_output=True, text=True, errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-16"},
        cwd=str(ROOT), timeout=180,
    )
    assert child.returncode == 0, child.stderr
    assert "KEPT=utf-16" in child.stdout


def test_a_blank_value_counts_as_unset():
    """`PYTHONIOENCODING=""` is ignored by Python and falls back to the code
    page, but `setdefault` sees the key as present and leaves it -- which would
    hand every spawned worker exactly the encoding this exists to avoid. A shell
    or a container can pass a blank value through without anyone meaning to.

    Empty specifically, not whitespace: a whitespace value makes the interpreter
    refuse to start at all ("unknown encoding"), so it never reaches this code.
    """
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys, os; sys.path.insert(0, '.');"
         "import control_plane.runner.entrypoint;"
         "print('CHILDREN_GET=' + os.environ.get('PYTHONIOENCODING', 'unset'))"],
        capture_output=True, text=True, errors="replace",
        env={**os.environ, "PYTHONIOENCODING": ""},
        cwd=str(ROOT), timeout=180,
    )
    assert child.returncode == 0, child.stderr
    assert "CHILDREN_GET=utf-8" in child.stdout
