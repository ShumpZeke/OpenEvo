#!/usr/bin/env bash
# Run the same task with each optional behaviour on and off, and report whether
# it helped. Requires the control plane (./run.sh).
#
#   ./scripts/ablation.sh --arms operators,island_policies --repeats 2
set -euo pipefail
cd "$(dirname "$0")/.."
exec "$PWD/.venv/bin/python" scripts/ablation.py "$@"
