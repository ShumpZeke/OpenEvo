#!/usr/bin/env bash
# Live terminal dashboard: providers, rate windows, routes, evolution state.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "$PWD/.venv/bin/python" -m oe_max.dashboard "$@"
