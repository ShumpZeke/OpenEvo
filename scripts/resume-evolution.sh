#!/usr/bin/env bash
# Resume from the newest checkpoint of a previous run directory.
#   ./scripts/resume-evolution.sh runs/20260826-001122-function_minimization-max
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$PWD/.venv/bin/python"
RUN="${1:-}"
[ -n "$RUN" ] || { echo "usage: $0 <run_dir> [--iterations N]"; exit 2; }
[ -d "$RUN/checkpoints" ] || { echo "no checkpoints in $RUN"; exit 1; }
shift || true
ITER=20
[ "${1:-}" = "--iterations" ] && { ITER="$2"; shift 2; }

CKPT=$(ls -d "$RUN"/checkpoints/checkpoint_* 2>/dev/null \
  | sed 's/.*checkpoint_//' | sort -n | tail -1)
[ -n "$CKPT" ] || { echo "no checkpoint found"; exit 1; }
echo "resuming $RUN from checkpoint_$CKPT"
exec "$PY" openevolve-run.py \
  examples/function_minimization/initial_program.py \
  examples/function_minimization/evaluator.py \
  --config configs/oe_max/evolution.yaml \
  --checkpoint "$RUN/checkpoints/checkpoint_$CKPT" \
  --iterations "$ITER" --output "$RUN"
