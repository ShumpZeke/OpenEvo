#!/usr/bin/env bash
# Resume from the newest checkpoint of a previous run directory.
#   ./scripts/resume-evolution.sh runs/20260826-001122-function_minimization-max
#   ./scripts/resume-evolution.sh runs/my-run --task circle_packing --iterations 10
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./bootstrap.sh first."; exit 1; }

RUN="${1:-}"
[ -n "$RUN" ] || { echo "usage: $0 <run_dir> [--task NAME] [--iterations N]"; exit 2; }
[ -d "$RUN/checkpoints" ] || { echo "no checkpoints in $RUN"; exit 1; }
shift || true

ITER=20
TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --iterations) ITER="$2"; shift 2 ;;
    --task)       TASK="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

# The task used to be hardcoded to function_minimization, so resuming any other
# run silently continued it against the WRONG program and evaluator — the
# scores kept coming and measured a different problem. Recover it from the run
# directory's own name, which `run-evolution.sh` builds as
# `runs/<timestamp>-<task>-<profile>`, and let --task override when the
# directory was named by hand.
if [ -z "$TASK" ]; then
  BASE="$(basename "$RUN")"
  GUESS="$(printf '%s' "$BASE" | sed -E 's/^[0-9]{8}-[0-9]{6}-//; s/-(max|stock)$//')"
  if [ -n "$GUESS" ] && [ -d "examples/$GUESS" ]; then
    TASK="$GUESS"
  else
    TASK="function_minimization"
    echo "note: could not infer the task from '$BASE'; assuming $TASK." >&2
    echo "      pass --task NAME if that is wrong — resuming with the wrong" >&2
    echo "      evaluator produces scores for a different problem." >&2
  fi
fi

PROG="examples/$TASK/initial_program.py"
EVAL="examples/$TASK/evaluator.py"
for f in "$PROG" "$EVAL"; do
  [ -f "$f" ] || { echo "missing: $f (is --task $TASK right?)"; exit 1; }
done

CKPT=$(ls -d "$RUN"/checkpoints/checkpoint_* 2>/dev/null \
  | sed 's/.*checkpoint_//' | sort -n | tail -1)
[ -n "$CKPT" ] || { echo "no checkpoint found"; exit 1; }

# Through the instrumented entrypoint, not `openevolve-run.py`. The plain CLI
# installs no telemetry, and every OE-MAX feature is installed BY that
# telemetry — so a resumed run silently lost operator steering, attribution and
# the rest, and emitted no events at all. See HANDOFF §3.11.
export EVOLUTION_RUN_ID="${EVOLUTION_RUN_ID:-run_$(date +%Y%m%d%H%M%S)_$$}"
export EVOLUTION_TELEMETRY="${EVOLUTION_TELEMETRY:-1}"
export EVOLUTION_EVENT_LOG="${EVOLUTION_EVENT_LOG:-$RUN/events.ndjson}"
# So the memory journal can link a resumed run back to the one it continues.
export EVOLUTION_RESUMED_FROM="$RUN/checkpoints/checkpoint_$CKPT"

echo "resuming $RUN from checkpoint_$CKPT (task=$TASK, iterations=$ITER)"
echo "run_id=$EVOLUTION_RUN_ID"
exec "$PY" -m control_plane.runner.entrypoint "$PROG" "$EVAL" \
  --config configs/oe_max/evolution.yaml \
  --checkpoint "$RUN/checkpoints/checkpoint_$CKPT" \
  --iterations "$ITER" --output "$RUN"
