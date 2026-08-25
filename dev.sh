#!/usr/bin/env bash
# Evolution — development servers (API with reload + Vite dev server).
set -euo pipefail
cd "$(dirname "$0")"
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./bootstrap.sh first."; exit 1; }

"$PY" -m uvicorn "control_plane.api.app:create_app" --factory \
  --host 127.0.0.1 --port "${EVOLUTION_PORT:-8000}" --reload &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT

if [ -d web/node_modules ]; then
  (cd web && npm run dev)
else
  echo "web/node_modules missing — run ./bootstrap.sh"
  wait $API
fi
