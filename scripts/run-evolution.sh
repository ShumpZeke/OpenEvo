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

echo "task=$TASK profile=$PROFILE iterations=$ITER"
echo "config=$CFG"
echo "output=$OUT"
exec "$PY" openevolve-run.py "$PROG" "$EVAL" \
  --config "$CFG" --iterations "$ITER" --output "$OUT"
