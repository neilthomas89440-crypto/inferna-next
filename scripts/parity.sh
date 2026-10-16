#!/usr/bin/env bash
# Parity verdict: one command per configuration, comparing against the single-node baseline.
#
#   scripts/parity.sh --mode smoke  --api http://localhost:8000 --api-key "$KEY" --model Qwen/…
#   scripts/parity.sh --mode single --admin-token "$JWT" --api-key "$KEY" --gpus 8 \
#                     --model Qwen/… --baseline artifacts/parity-baseline.jsonl --environment hardware
#   scripts/parity.sh --mode multi  --admin-token "$JWT" --api-key "$KEY" --nodes 2 --gpus 4 \
#                     --mode-flag pp --baseline artifacts/parity-baseline.jsonl --environment hardware
#   scripts/parity.sh --mode full   …      # 1x8 baseline, 2x4 pp, 2x4 tp, 4x2 pp
#
# The math lives in the bench project (`bench/bench/parity.py`); this wrapper only checks the
# toolchain. Exit codes: 0 ok, 1 a cell exceeded the error budget, 2 prerequisites missing — the
# last one is what a stack without Docker/GPUs returns, so CI can tell "cannot measure" from "bad".
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

export PYTHONPATH="${PWD}/bench${PYTHONPATH:+:${PYTHONPATH}}"
exec uv run --project bench python -m bench.parity "$@"