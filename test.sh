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

echo "=== upstream OpenEvolve compatibility suite ==="
"$PY" -m pytest tests/ -q -m "not slow" \
  --ignore=tests/evolution --ignore=tests/oe_max || RC=1

echo
echo "=== control plane ==="
"$PY" -m pytest tests/evolution -q || RC=1

echo
echo "=== OE-MAX (broker, limiter, gates, search) ==="
"$PY" -m pytest tests/oe_max -q || RC=1

echo
if [ -d web/node_modules ]; then
  echo "=== web typecheck ==="
  (cd web && npm run typecheck) || RC=1
fi

echo
[ "$RC" -eq 0 ] && echo "All suites passed." || echo "Failures reported above."
exit "$RC"
