"""
The upstream engine must stay byte-identical to the commit it was forked from.

That invariant is what makes an upstream merge a fast-forward instead of a
conflict resolution. It is rule 1 in CLAUDE.md and is recorded in
docs/patch-surface.md as "no files touched". It is also the rule most easily broken
by accident: dropping one new module under `openevolve/` looks harmless and
quietly ends the guarantee, because a patch surface is only empty until it is
not.

Behaviour is added by wrapping public methods at runtime instead — see
`control_plane/telemetry/instrument.py` for the pattern, and `oe_max/brain/llm.py`
for an upstream interface implemented from outside the tree.
"""

import subprocess

import pytest

ENGINE = "openevolve"


def _git(*args):
    return subprocess.run(("git",) + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def porcelain():
    probe = _git("rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        pytest.skip("not a git checkout, so the patch surface cannot be read")
    status = _git("status", "--porcelain", "--", ENGINE)
    if status.returncode != 0:
        pytest.skip("git status failed: " + status.stderr.strip())
    return [line for line in status.stdout.splitlines() if line.strip()]


def test_the_engine_tree_has_no_modified_files(porcelain):
    """
    A tracked file under `openevolve/` that differs from HEAD is a patch, and
    the fork claims to carry none.
    """
    changed = [line for line in porcelain if not line.startswith("??")]
    assert not changed, (
        "openevolve/ is modified, which ends the empty patch surface: "
        + "; ".join(changed)
        + " -- wrap the behaviour at runtime instead, or record the edit in "
          "docs/patch-surface.md with the reason."
    )


def test_the_engine_tree_has_no_added_files(porcelain):
    """
    A *new* file is the easier mistake to make and the harder one to see: it
    conflicts with nothing on merge, so the fork keeps merging cleanly right up
    until upstream adds a file at the same path.
    """
    added = [line for line in porcelain if line.startswith("??")]
    assert not added, (
        "openevolve/ holds files upstream does not: "
        + "; ".join(added)
        + " -- code implementing an upstream interface can live outside the "
          "tree; see oe_max/brain/llm.py."
    )
