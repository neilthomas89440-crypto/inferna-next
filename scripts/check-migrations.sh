#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../server"

PREVIOUS_RELEASE_HEAD="3b491ceedb36"  # при следующем релизе заменить на новый head

echo "==> upgrade head (empty DB)"
uv run alembic upgrade head

echo "==> downgrade base + upgrade head (full down/up cycle)"
uv run alembic downgrade base && uv run alembic upgrade head

echo "==> upgrade from previous-release schema with legacy data"
uv run alembic downgrade base
uv run alembic upgrade "$PREVIOUS_RELEASE_HEAD"

# Insert legacy data directly via asyncpg
uv run python - <<'PY'
import asyncio
import os
import uuid

url = os.getenv("INFERNA_DATABASE_URL", "sqlite+aiosqlite:///./inferna.db")
if url.startswith("sqlite"):
    print("sqlite URL, skipping legacy insert")
    raise SystemExit(0)
# strip driver prefix for asyncpg
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]
elif url.startswith("postgresql://"):
    pass
else:
    print(f"unsupported URL {url}, skipping")
    raise SystemExit(0)

import asyncpg

async def main():
    conn = await asyncpg.connect(url)
    try:
        legacy_id = str(uuid.uuid4())
        # use native UUID type? clusters.id is UUID; asyncpg handles str as uuid
        await conn.execute(
            "INSERT INTO clusters (id, name, description, created_at) VALUES ($1, 'legacy-cluster', NULL, now())",
            legacy_id,
        )
        print(f"inserted legacy cluster {legacy_id}")
    finally:
        await conn.close()

asyncio.run(main())
PY

echo "==> upgrade head from previous-release"
uv run alembic upgrade head
# Verify legacy data survived
uv run python - <<'PY'
import asyncio
import os

url = os.getenv("INFERNA_DATABASE_URL", "sqlite+aiosqlite:///./inferna.db")
if url.startswith("sqlite"):
    print("sqlite URL, skipping check")
    raise SystemExit(0)
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]
elif url.startswith("postgresql://"):
    pass
else:
    print(f"unsupported URL {url}, skipping")
    raise SystemExit(0)

import asyncpg

async def main():
    conn = await asyncpg.connect(url)
    try:
        count = await conn.fetchval("SELECT count(*) FROM clusters WHERE name='legacy-cluster'")
        print(f"legacy count: {count}")
        if count != 1:
            print(f"ERROR: expected 1 legacy cluster, got {count}")
            raise SystemExit(1)
    finally:
        await conn.close()

asyncio.run(main())
PY

echo "OK: migration check passed"
