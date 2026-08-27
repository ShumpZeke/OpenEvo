#!/usr/bin/env bash
# Evolution — full test suite.
#
# Runs the preserved upstream tests first: a change that breaks them is a
# regression in the fork, not merely a control-plane bug.
set -euo pipefail
cd "$(dirname "$0")"
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./bootstrap.sh first."; exit 1; }
RC=0

# Nothing of ours lives in tests/ root -- every file there is upstream's,
# which is what lets a failure here be read as "the fork broke the engine"
# rather than "one of our own tests is red". Fork-side suites each get
# their own directory.
echo "=== upstream OpenEvolve compatibility suite ==="
"$PY" -m pytest tests/ -q -m "not slow" \
  --ignore=tests/evolution --ignore=tests/oe_max --ignore=tests/brain || RC=1

echo
echo "=== control plane ==="
"$PY" -m pytest tests/evolution -q || RC=1

echo
echo "=== OE-MAX (broker, limiter, gates, search) ==="
"$PY" -m pytest tests/oe_max -q || RC=1

echo
echo "=== BrainPort (OpenCode brain, worker, plugin contract) ==="
"$PY" -m pytest tests/brain -q || RC=1

echo
if [ -d web/node_modules ]; then
  echo "=== web typecheck ==="
  (cd web && npm run typecheck) || RC=1
fi

echo
[ "$RC" -eq 0 ] && echo "All suites passed." || echo "Failures reported above."
exit "$RC"
