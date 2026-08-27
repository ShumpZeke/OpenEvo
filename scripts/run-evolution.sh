#!/usr/bin/env bash
# Terminal 2: run evolution through the broker.
#   ./scripts/run-evolution.sh [--task function_minimization] [--iterations 20]
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./bootstrap.sh first."; exit 1; }

TASK="function_minimization"; ITER=20; PROFILE="max"; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --task)       TASK="$2"; shift 2 ;;
    --iterations) ITER="$2"; shift 2 ;;
    --profile)    PROFILE="$2"; shift 2 ;;
    --output)     OUT="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

case "$PROFILE" in
  max)   CFG="configs/oe_max/evolution.yaml" ;;
  # Baseline arm: same model, same settings, but talking to the provider
  # directly instead of through the broker. That isolates the system from the
  # model, which is what makes the comparison meaningful.
  stock) CFG="configs/oe_max/stock_baseline.yaml" ;;
  *)     CFG="$PROFILE" ;;                      # explicit path
esac

PROG="examples/$TASK/initial_program.py"
EVAL="examples/$TASK/evaluator.py"
for f in "$PROG" "$EVAL" "$CFG"; do
  [ -f "$f" ] || { echo "missing: $f"; exit 1; }
done
OUT="${OUT:-runs/$(date +%Y%m%d-%H%M%S)-$TASK-$PROFILE}"
mkdir -p "$OUT"

# Fail fast with a clear message rather than a confusing connection error.
if [ "$PROFILE" = "max" ] && ! curl -sf --max-time 5 \
     "http://127.0.0.1:${OE_MAX_PORT:-8787}/health" >/dev/null; then
  echo "The broker is not running. Start it first:  ./scripts/start-broker.sh"
  exit 1
fi

# Run through the instrumented entrypoint, not `openevolve-run.py` directly.
#
# This script used to exec the plain upstream CLI, which installs no telemetry
# at all — and every OE_MAX_* feature is installed BY that telemetry. So
# `OE_MAX_OPERATORS=1 ./scripts/run-evolution.sh` set an environment variable
# that nothing ever read: no operator steering, no attribution, no
# multi-offspring, no verification, no bandit. The run succeeded and the
# feature silently did not happen, which is the worst way for a flag to fail.
#
# The entrypoint requires a run id, so generate one when the caller has not.
export EVOLUTION_RUN_ID="${EVOLUTION_RUN_ID:-run_$(date +%Y%m%d%H%M%S)_$$}"
export EVOLUTION_TELEMETRY="${EVOLUTION_TELEMETRY:-1}"
export EVOLUTION_EVENT_LOG="${EVOLUTION_EVENT_LOG:-$OUT/events.ndjson}"

echo "task=$TASK profile=$PROFILE iterations=$ITER"
echo "config=$CFG"
echo "output=$OUT"
echo "run_id=$EVOLUTION_RUN_ID"
echo "events=$EVOLUTION_EVENT_LOG"
exec "$PY" -m control_plane.runner.entrypoint "$PROG" "$EVAL" \
  --config "$CFG" --iterations "$ITER" --output "$OUT"
