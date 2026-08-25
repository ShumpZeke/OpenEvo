#!/usr/bin/env bash
# Evolution — reproducible bootstrap (Linux/macOS).
#
# Detects the toolchain, creates a project-local Python environment, installs
# the engine and control plane, builds the web UI, and runs doctor checks.
# Nothing is installed globally and nothing outside this directory is modified.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
VENV="$ROOT/.venv"
FAIL=0

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$*"; FAIL=1; }

say "Detecting platform"
ok "$(uname -s) $(uname -m)"

say "Checking required tools"
need() {
  if command -v "$1" >/dev/null 2>&1; then ok "$1 — $($1 --version 2>&1 | head -1)"
  else bad "$1 not found${2:+ ($2)}"; fi
}
need git
need python3
if ! command -v node >/dev/null 2>&1; then
  warn "node not found — the web UI cannot be built (the API still runs)"
else ok "node — $(node --version)"; fi

say "Creating Python environment"
if command -v uv >/dev/null 2>&1; then
  uv venv "$VENV" --python 3.11 >/dev/null 2>&1 || uv venv "$VENV" >/dev/null
  PIP=(uv pip install --python "$VENV/bin/python")
else
  python3 -m venv "$VENV"
  PIP=("$VENV/bin/pip" install)
fi
ok "$VENV"

say "Installing the engine and control plane"
"${PIP[@]}" -e . >/dev/null
ok "openevolve (engine) + control plane installed"

say "Initialising control-plane storage"
mkdir -p "$ROOT/.evolution/workspace"
"$VENV/bin/python" - <<'PY'
import sys, os
sys.path.insert(0, os.getcwd())
from control_plane.storage.store import Store
s = Store(os.path.join(".evolution", "workspace", "control_plane.db"))
print(f"  schema ready: {s.path}")
s.close()
PY
ok "storage initialised"

say "Checking the agent sandbox (OpenCode isolation)"
"$VENV/bin/python" - <<'PY'
import sys, os
sys.path.insert(0, os.getcwd())
from control_plane.sandbox.opencode import OpenCodeIsolation
r = OpenCodeIsolation(os.path.join(os.getcwd(), ".evolution", "workspace")).preflight()
print(f"  isolation level : {r.level.value}")
print(f"  opencode binary : {r.binary or 'not found'}")
print(f"  container       : {'docker' if r.docker_available else 'none'}")
print(f"  oh-my-openagent : {r.omo.get('binary') or 'not installed (optional)'}")
for w in r.warnings: print(f"  note: {w}")
for x in r.reasons:  print(f"  reason: {x}")
PY

say "Building the web Control Center"
if command -v npm >/dev/null 2>&1; then
  (cd web && npm install --no-audit --no-fund >/dev/null 2>&1 && npm run build >/dev/null 2>&1) \
    && ok "web/dist built" || warn "web build failed — the API will serve without a UI"
else
  warn "npm unavailable — skipping the web build"
fi

say "Creating .env from the example (no secrets are written)"
[ -f .env ] || { cp .env.example .env; ok ".env created — add provider keys to enable live routes"; }

say "Running the smoke test"
if "$VENV/bin/python" -m pytest tests/evolution -q >/dev/null 2>&1; then
  ok "control-plane tests passed"
else
  warn "control-plane tests reported failures — run ./test.sh for detail"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  say "Bootstrap complete"
else
  say "Bootstrap finished with missing prerequisites (see ✗ above)"
fi
cat <<'EOF'

  Start everything      ./run.sh
  Control Center        http://127.0.0.1:8000
  Classic visualizer    ./run.sh classic <checkpoint_dir>   → http://127.0.0.1:8080
  Original CLI          .venv/bin/openevolve-run --help
  Tests                 ./test.sh
  Dev servers           ./dev.sh

EOF
exit "$FAIL"
