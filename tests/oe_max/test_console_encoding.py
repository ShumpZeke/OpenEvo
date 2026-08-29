"""A CLI must not be killed by a character in its own banner.

Windows gives a child the console code page — cp1252 here — and printing a
character it cannot encode raises `UnicodeEncodeError`. Not mangled output: an
exception, on the print, which for anything without a handler means the process
dies.

`oe_max/broker/cli.py` printed a startup line containing an arrow, so **the
broker exited 1 before uvicorn started** whenever stdout was not already UTF-8:
every redirected start, every service wrapper, every `> broker.log`. It was
found by trying to start the broker. An audit then found ten such call sites
across five fork-owned files — and the audit script itself crashed printing its
own results.
"""

import ast
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from oe_max.console import use_utf8_stdio

ROOT = Path(__file__).resolve().parents[2]

# Directories that are upstream's or not ours to change.
SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "build", "dist", ".evolution", "openevolve", "examples", "tests", "upstream",
}

ARROW = "→"


def _fork_owned_python_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield Path(root) / name


def _prints_non_ascii(source):
    """True if any `print(...)` call in `source` contains a non-ASCII character."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            segment = ast.get_source_segment(source, node) or ""
            if any(ord(c) > 127 for c in segment):
                return True
    return False


def test_every_file_that_prints_non_ascii_protects_itself():
    """The audit, as a test, so new code cannot quietly reintroduce this.

    Ten call sites existed across five files. Adding an em dash to a `print` in
    an unprotected entry point is an easy and invisible way to make it die on
    Windows, and nothing else in the suite would catch it.
    """
    offenders = []
    for path in _fork_owned_python_files():
        source = io.open(path, encoding="utf-8").read()
        if _prints_non_ascii(source) and "use_utf8_stdio" not in source:
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        "these print non-ASCII without calling use_utf8_stdio(), so they die on "
        "a cp1252 console: " + ", ".join(sorted(offenders))
    )


def test_it_reconfigures_the_streams():
    use_utf8_stdio()
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if encoding is not None:  # pytest capture may not expose one
            assert encoding.lower().replace("-", "") == "utf8", encoding


def test_it_is_safe_when_the_stream_cannot_be_reconfigured(monkeypatch):
    """Under pytest capture, and behind pipes, the streams are not always
    TextIOWrapper. A defensive helper that raises is worse than none."""
    class NoReconfigure:
        encoding = "cp1252"

        def write(self, text):
            return len(text)

    monkeypatch.setattr(sys, "stdout", NoReconfigure())
    monkeypatch.setattr(sys, "stderr", NoReconfigure())
    use_utf8_stdio()  # must not raise


def test_it_is_idempotent():
    use_utf8_stdio()
    use_utf8_stdio()


@pytest.mark.parametrize("module", [
    "oe_max.broker.cli",
    "control_plane.api.cli",
])
def test_the_clis_survive_a_legacy_code_page(module):
    """The actual regression: run each CLI under cp1252 with stdout to a pipe.

    `--help` exits before binding a port or starting a server, so this exercises
    the import and the banner path without leaving anything running.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    completed = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True, text=True, errors="replace", env=env,
        cwd=str(ROOT), timeout=180,
    )
    assert "UnicodeEncodeError" not in completed.stderr, completed.stderr
    assert completed.returncode == 0, completed.stderr


def test_the_broker_banner_still_contains_the_arrow():
    """The fix is "make the stream carry it", not "delete the character".

    Replacing it with ASCII would also pass the test above and would quietly
    accept that this repo cannot print non-ASCII — which is not true and would
    keep being rediscovered.
    """
    source = (ROOT / "oe_max" / "broker" / "cli.py").read_text(encoding="utf-8")
    assert ARROW in source
    assert "use_utf8_stdio()" in source
