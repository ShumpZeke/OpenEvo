#!/usr/bin/env bash
# Live provider verification: discover models, then prove which actually serve.
# Listing a model is not the same as serving it — Zen lists deepseek-v4-flash-free
# and then returns "Model is unavailable" for it.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${OE_MAX_PORT:-8787}"
curl -sf --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null \
  || { echo "Broker not running. Start it:  ./scripts/start-broker.sh"; exit 1; }
curl -s --max-time 600 -X POST \
  "http://127.0.0.1:$PORT/v1/oe-max/verify?check_tools=true" \
  | "$PWD/.venv/bin/python" "$PWD/scripts/_print_probes.py"
