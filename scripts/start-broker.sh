#!/usr/bin/env bash
# Terminal 1: the OE-MAX provider broker.
# OpenEvolve points at this; upstream credentials never leave this process.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./bootstrap.sh first."; exit 1; }
[ -f .env ] && set -a && . ./.env && set +a || true
exec "$PY" -m oe_max.broker.cli --port "${OE_MAX_PORT:-8787}" "$@"
