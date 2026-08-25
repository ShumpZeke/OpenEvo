#!/usr/bin/env bash
# Evolution — launch the control plane, or the classic visualizer.
set -euo pipefail
cd "$(dirname "$0")"
VENV="$PWD/.venv"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "No environment found. Run ./bootstrap.sh first."; exit 1; }

case "${1:-server}" in
  classic)
    # Upstream's own visualizer, launched unmodified.
    CKPT="${2:-}"
    [ -n "$CKPT" ] || { echo "usage: ./run.sh classic <checkpoint_dir>"; exit 2; }
    exec "$PY" scripts/visualizer.py --path "$CKPT" --port "${EVOLUTION_CLASSIC_PORT:-8080}"
    ;;
  provider)
    # Local OpenAI-compatible endpoint for offline testing.
    exec "$PY" scripts/local_provider.py --port "${2:-8765}"
    ;;
  cli)
    shift
    exec "$VENV/bin/openevolve-run" "$@"
    ;;
  server|*)
    PORT="${EVOLUTION_PORT:-8000}"
    echo "Evolution Control Center → http://127.0.0.1:${PORT}"
    exec "$PY" -m uvicorn "control_plane.api.app:create_app" \
      --factory --host "${EVOLUTION_HOST:-127.0.0.1}" --port "$PORT"
    ;;
esac
