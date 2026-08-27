"""
The plugin entry point named in `opencode.json` must actually be loadable.

This exists because it was not. `.opencode/plugins/openevo.js` held a compiled
copy of `src/index.ts` whose `import "./worker.js"` and
`import "./brain-bridge.js"` resolved against a directory containing only that
one file, so OpenCode could never load the plugin -- and every test that
checked "the plugin exposes the right tools" passed anyway, because they all
read `src/`, which was fine.

That is the gap: the source being correct says nothing about the file OpenCode
is pointed at. These tests check the wiring rather than the implementation.
"""

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG = REPO / "opencode.json"

# `from "..."` / `import "..."` targets in an ES module.
IMPORT_TARGET = re.compile(r"""(?:from|import)\s*\(?\s*["']([^"']+)["']""")

# Comments are stripped first. This file's own prose quotes the broken imports
# it exists to catch, and a scanner that reads comments flags the explanation.
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _code_only(source: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source))


@pytest.fixture(scope="module")
def entry_path():
    assert CONFIG.exists(), "opencode.json is missing"
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    plugins = config.get("plugin") or []
    assert plugins, "opencode.json declares no plugin"

    resolved = []
    for rel in plugins:
        # Not rel.lstrip("./") -- lstrip takes a character *set*, so it eats the
        # leading dot of ".opencode" too. pathlib normalises "./" on resolve().
        path = (REPO / rel).resolve()
        assert path.exists(), f"opencode.json names {rel}, which does not exist"
        resolved.append(path)
    assert len(resolved) == 1, "expected exactly one plugin entry point"
    return resolved[0]


def test_the_entry_points_relative_imports_resolve(entry_path):
    """
    Every relative import in the entry file must exist on disk.

    A bare-specifier import (`@opencode-ai/plugin`) is resolved by node from
    node_modules and is not this test's business. A *relative* one is a file
    that either sits next to the entry or does not, and when it does not the
    plugin fails at load with an error naming a path nobody created.
    """
    source = _code_only(entry_path.read_text(encoding="utf-8"))
    missing = []
    for target in IMPORT_TARGET.findall(source):
        if not target.startswith("."):
            continue
        candidate = (entry_path.parent / target).resolve()
        if not candidate.exists():
            missing.append(f"{target} -> {candidate}")

    assert not missing, (
        f"{entry_path.name} imports files that are not there: "
        + "; ".join(missing)
        + " -- the entry point should delegate to the built plugin rather than "
          "be a copy of it."
    )


def test_the_entry_delegates_rather_than_duplicating(entry_path):
    """
    Two copies of the plugin drift, and the one OpenCode loads is the one
    nobody edits. The entry is a shim; the implementation lives in
    packages/opencode-plugin/src.
    """
    source = entry_path.read_text(encoding="utf-8")
    assert "packages/opencode-plugin/dist" in source, (
        "the entry point does not forward to the built plugin; if it has been "
        "inlined again, the copy will drift from src/"
    )
    # A shim is short. A copy of the compiled plugin is not.
    code_lines = [
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith(("*", "/*", "//", "*/"))
    ]
    assert len(code_lines) < 60, (
        f"{entry_path.name} has {len(code_lines)} lines of code, which is a "
        "copy of the plugin rather than a shim"
    )


def test_the_entry_exports_the_plugin_hook(entry_path):
    source = entry_path.read_text(encoding="utf-8")
    assert "OpenEvoPlugin" in source, "entry point exports no OpenEvoPlugin"
