#!/usr/bin/env bash
# Thin wrapper around the benchmark harness.
#
#   scripts/bench.sh --api http://localhost:8000 --api-key "$INFERNA_API_KEY" \
#                    --model Qwen/Qwen2.5-0.5B-Instruct --scenario single --repeats 3 --warmup 1
#
# The harness lives in its own uv project (bench/), so this script only checks the toolchain
# and forwards every argument. See docs/multinode-bench.md for recorded runs.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://docs.astral.sh/uv/)" >&2
  exit 2
fi

if [ ! -d bench ]; then
  echo "bench/ project not found" >&2
  exit 2
fi

exec uv run --project bench --directory bench python -m bench "$@"