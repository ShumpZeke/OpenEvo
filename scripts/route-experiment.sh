#!/usr/bin/env bash
# Run the same task on each of several routes and compare their mutation
# quality. Requires the broker (./scripts/start-broker.sh) and the control
# plane (./run.sh) to be up.
#
#   ./scripts/route-experiment.sh --routes x-preview-f-free,nemotron-3-ultra-free \
#                                 --iterations 12 --repeats 2
set -euo pipefail
cd "$(dirname "$0")/.."
exec "$PWD/.venv/bin/python" scripts/route_experiment.py "$@"
