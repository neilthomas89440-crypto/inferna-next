#!/usr/bin/env bash
# One-time local setup: Python 3.12 via uv, server/worker deps, protobuf stubs,
# frontend deps, .env, database migrations.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Python 3.12"
uv python install 3.12

echo "==> server deps"
(cd server && uv sync --dev)

echo "==> worker deps"
(cd worker && uv sync --dev)

echo "==> protobuf stubs"
bash scripts/gen-proto.sh

echo "==> frontend deps"
(cd frontend && npm install)

echo "==> .env"
if [ -f .env ]; then
  echo ".env exists, leaving it alone"
else
  cp .env.example .env
  echo ".env created from .env.example"
fi

echo "==> migrations"
(cd server && uv run alembic upgrade head)

echo "OK: dev setup complete"
