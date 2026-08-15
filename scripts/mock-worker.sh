#!/usr/bin/env bash
# Run the worker in mock mode (no GPU, no docker) — the demo/dev path.
set -euo pipefail

cd "$(dirname "$0")/../worker"
INFERNA_MOCK_ENGINE=true uv run python -m inferna_worker
