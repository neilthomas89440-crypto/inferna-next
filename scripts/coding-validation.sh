#!/usr/bin/env bash
# Coding validation: run the deterministic task set through /v1 and score the answers.
#
#   scripts/coding-validation.sh --api http://server:8000 --api-key "$KEY" \
#       --model Qwen/Qwen2.5-Coder-7B-Instruct --environment hardware
#   scripts/coding-validation.sh --api http://localhost:8000 --api-key "$KEY" --model … \
#       --environment mock --only gen-func-body,json-schema-answer     # harness check
#   scripts/coding-validation.sh … --allow-code-execution              # also run the pytest tasks
#
# Text checks always run; the two `pytest` tasks write the answer to a temporary directory and
# execute pytest there, so they are opt-in. Exit codes: 0 ok, 1 failures on hardware, 2 failures on
# a non-hardware stack or missing prerequisites (see the runner's docstring).
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://docs.astral.sh/uv/)" >&2
  exit 2
fi

if [ ! -f coding/tasks.json ]; then
  echo "coding/tasks.json not found" >&2
  exit 2
fi

export PYTHONPATH="${PWD}/bench${PYTHONPATH:+:${PYTHONPATH}}"
exec uv run --project bench python -m bench.coding "$@"