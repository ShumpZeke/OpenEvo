"""Make this process's stdout able to carry the characters it prints.

Windows gives a child the console code page — cp1252 here — and `print` of a
character it cannot encode raises `UnicodeEncodeError`. Not mangled output: an
exception, on the print, which for anything without a handler around it means
the process dies.

This is not hypothetical and it is not cosmetic. `oe_max/broker/cli.py` printed
a startup banner containing an arrow, so the **broker exited 1 before uvicorn
started** whenever stdout was not already UTF-8 — which is every redirected
start, every service wrapper, and every `> broker.log`. `control_plane/api/cli.py`
had the same line. An audit found ten such call sites across five fork-owned
files, and the audit script itself crashed printing its results.

Call this first thing in any entry point that prints. It is cheaper than
auditing every string, and unlike replacing the characters with ASCII it stays
fixed when someone adds a new one.

The runner sets `PYTHONIOENCODING=utf-8` for evolution subprocesses, which
solves the same problem from the other side. That does not help a CLI the
operator starts themselves, which is what this is for.
"""

from __future__ import annotations

import sys

__all__ = ["use_utf8_stdio"]


def use_utf8_stdio(errors: str = "replace") -> None:
    """Reconfigure stdout and stderr to UTF-8, quietly doing nothing if it cannot.

    ``errors="replace"`` rather than ``"strict"`` so that a stream which somehow
    still cannot represent a character degrades to a question mark instead of
    raising. A banner is not worth a crash either way round.

    Safe to call more than once, and safe under pytest's capture, which replaces
    the streams with objects that may not offer ``reconfigure``.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (ValueError, OSError, AttributeError):
            # A detached, closed or non-text stream. Printing ASCII still works,
            # and raising here would defeat the point of a defensive helper.
            pass
