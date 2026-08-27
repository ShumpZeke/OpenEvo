"""
Legacy Deletion Readiness Gate

Answers one question: can `oe_max/providers`, `oe_max/router`, `oe_max/limiter`
and `control_plane/providers` be deleted without breaking anything?

There are two ways to depend on the legacy provider stack and this checks both,
because an earlier version checked only the first and reported "safe to delete"
while the entire shipping run path still ran on it:

  1. Import coupling -- a module does `from oe_max.providers import ...`, or
     hardcodes a provider model ID or endpoint URL.

  2. Runtime coupling -- nothing imports the broker at all, because the engine
     talks to it over HTTP. `configs/oe_max/evolution.yaml` points `api_base` at
     127.0.0.1:8787, `scripts/start-broker.sh` launches it, and `pyproject.toml`
     ships console entry points for it. Grepping for imports finds none of that
     and concludes the coast is clear.

A clean import scan means the *core* no longer reaches into the legacy stack. It
does not mean the stack is unused. Only the second scan can say that, so the
verdict below refuses to say "safe to delete" until both are clear.

Usage:
    python scripts/legacy_deletion_gate.py [--repo-root PATH]

Exit codes:
    0 = READY   (no coupling of either kind; the legacy stack can be removed)
    1 = BLOCKED (something still depends on it; the report says what)
    2 = the repository root could not be determined
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Files and directories that ARE the legacy stack, or are its sanctioned
# adapter. They are expected to reference it and are not scanned.
LEGACY_ALLOWED_REL: List[str] = [
    "oe_max/brain/legacy_adapter.py",
    "oe_max/providers/",
    "oe_max/router.py",
    "oe_max/limiter.py",
    "control_plane/providers/",
]

# The legacy stack itself, as deletion targets.
LEGACY_MODULES = [
    "oe_max.providers", "oe_max.router", "oe_max.limiter",
    "control_plane.providers", "oe_max.broker", "oe_max.dashboard",
]

LEGACY_IMPORT_PATTERNS: List[re.Pattern] = [
    re.compile(r"from\s+oe_max\.providers\b"),
    re.compile(r"import\s+oe_max\.providers\b"),
    re.compile(r"from\s+oe_max\.router\b"),
    re.compile(r"import\s+oe_max\.router\b"),
    re.compile(r"from\s+oe_max\.limiter\b"),
    re.compile(r"import\s+oe_max\.limiter\b"),
    re.compile(r"from\s+control_plane\.providers\b"),
    re.compile(r"import\s+control_plane\.providers\b"),
]

PROVIDER_MODEL_PATTERN = re.compile(
    r"""(?:["'`])(nvidia/|openrouter/|opencodezen/)[A-Za-z0-9._-]+(?:["'`])"""
)

PROVIDER_URL_PATTERNS: List[re.Pattern] = [
    re.compile(r"integrate\.api\.nvidia\.com"),
    re.compile(r"openrouter\.ai"),
    re.compile(r"api\.opencodezen\.ai"),
]

# The broker's address is the runtime coupling that no import scan can see.
BROKER_ENDPOINT = re.compile(r"127\.0\.0\.1:8787|localhost:8787|OE_MAX_PORT")
BROKER_LAUNCH = re.compile(r"oe_max\.broker|oe-max-broker|oe_max\.dashboard|oe-max-dashboard")

# control_plane is core: leaving it out is how the first version of this gate
# missed the runner entirely.
CORE_DIRS = ["oe_max", "openevolve", "control_plane", "benchmarks"]

# Where runtime coupling hides. Scanned as text, not as Python.
RUNTIME_GLOBS = ["configs/**/*.yaml", "configs/**/*.yml", "scripts/*.sh",
                 "scripts/*.ps1", "*.sh", "*.ps1", "pyproject.toml"]

SKIP_PARTS = {"__pycache__", "node_modules", ".venv", ".git"}


def is_legacy_allowed(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    for allowed in LEGACY_ALLOWED_REL:
        if allowed.endswith("/"):
            if normalized.startswith(allowed):
                return True
        elif normalized == allowed:
            return True
    return False


def _skip(path: Path, repo_root: Path) -> bool:
    parts = path.relative_to(repo_root).parts
    return any(p in SKIP_PARTS or p.startswith(".") for p in parts)


def scan_imports(repo_root: Path) -> List[Tuple[str, str, int, str]]:
    """Python-level coupling: imports, hardcoded model IDs, hardcoded URLs."""
    violations: List[Tuple[str, str, int, str]] = []

    for core_dir in CORE_DIRS:
        core_path = repo_root / core_dir
        if not core_path.exists():
            continue
        for filepath in sorted(core_path.rglob("*.py")):
            if _skip(filepath, repo_root) or filepath.name.startswith("test_"):
                continue
            rel = filepath.relative_to(repo_root).as_posix()
            if is_legacy_allowed(rel):
                continue

            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if any(pat.search(line) for pat in LEGACY_IMPORT_PATTERNS):
                    violations.append((rel, "legacy_import", i, stripped))
                    continue
                if PROVIDER_MODEL_PATTERN.search(line):
                    violations.append((rel, "provider_model_id", i, stripped))
                if any(pat.search(line) for pat in PROVIDER_URL_PATTERNS):
                    violations.append((rel, "provider_url", i, stripped))

    return violations


def scan_runtime(repo_root: Path) -> List[Tuple[str, str, int, str]]:
    """
    Configuration and operator scripts that route work through the broker.

    This is the scan that matters for deletion. None of it is an import, so the
    Python pass above sees a clean tree while every default run still depends on
    the process these files start.
    """
    couplings: List[Tuple[str, str, int, str]] = []
    seen = set()

    for pattern in RUNTIME_GLOBS:
        for filepath in sorted(repo_root.glob(pattern)):
            if not filepath.is_file() or _skip(filepath, repo_root):
                continue
            rel = filepath.relative_to(repo_root).as_posix()
            if rel in seen or is_legacy_allowed(rel):
                continue
            seen.add(rel)

            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                if BROKER_LAUNCH.search(line):
                    couplings.append((rel, "starts_or_ships_broker", i, stripped))
                elif BROKER_ENDPOINT.search(line):
                    couplings.append((rel, "points_at_broker", i, stripped))

    return couplings


def _report(title: str, rows: List[Tuple[str, str, int, str]]) -> None:
    by_file: Dict[str, List[Tuple[str, int, str]]] = {}
    for rel, kind, lno, text in rows:
        by_file.setdefault(rel, []).append((kind, lno, text))
    print(f"{title}: {len(rows)} in {len(by_file)} file(s)")
    for rel in sorted(by_file):
        print(f"  {rel}")
        for kind, lno, text in by_file[rel]:
            print(f"    L{lno:>5}  [{kind}]  {text[:110]}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy deletion readiness gate")
    parser.add_argument("--repo-root", type=Path, default=None)
    args = parser.parse_args()

    if args.repo_root:
        repo_root = args.repo_root.resolve()
    else:
        repo_root = Path(__file__).resolve().parent.parent
        if not (repo_root / "pyproject.toml").exists() and not (repo_root / ".git").exists():
            print(f"ERROR: cannot detect repo root from {Path(__file__).resolve().parent}")
            return 2

    print(f"Scanning {repo_root}")
    print(f"Deletion targets : {', '.join(LEGACY_MODULES)}")
    print(f"Not scanned      : {len(LEGACY_ALLOWED_REL)} legacy-owned paths")
    print()

    imports = scan_imports(repo_root)
    runtime = scan_runtime(repo_root)

    if imports:
        _report("IMPORT COUPLING", imports)
    else:
        print("IMPORT COUPLING: none. No core module imports the legacy stack,")
        print("  and no provider model ID or endpoint URL is hardcoded outside it.")
        print()

    if runtime:
        _report("RUNTIME COUPLING", runtime)
    else:
        print("RUNTIME COUPLING: none.")
        print()

    if not imports and not runtime:
        print("READY - nothing depends on the legacy stack by import or at runtime.")
        return 0

    print("BLOCKED - the legacy stack is still in use.")
    if imports:
        print("  Import coupling: remove these references, or move the file behind")
        print("  oe_max/brain/legacy_adapter.py.")
    if runtime:
        print("  Runtime coupling: these configs and scripts route work through the")
        print("  broker. Nothing imports it, so this is the coupling that a grep for")
        print("  imports will not show you. The default evolution path runs on it,")
        print("  and deleting the stack breaks that path even with a clean import scan.")
        print("  Move that path onto the BrainPort before deleting anything.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
