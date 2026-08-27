#!/usr/bin/env bash
# Project memory: where you left off, and what you noted.
#
#   ./scripts/memory.sh                          # the digest
#   ./scripts/memory.sh note "tried hy3 as reasoner" --kind decision
#   ./scripts/memory.sh search hy3
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./bootstrap.sh first."; exit 1; }
exec "$PY" -m control_plane.memory.cli "$@"
