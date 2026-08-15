"""Shared fixtures: in-memory SQLite (StaticPool), ASGI test client, seeded DB."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from inferna_server.auth import hash_password
from inferna_server.db import Base, get_db
from inferna_server.main import app
from inferna_server.models import Cluster, User, Worker, WorkerGPU
from inferna_server.services.workers_svc import seed_catalog, sha256_hex

TEST_ADMIN = {"username": "admin", "password": "inferna"}


@pytest.fixture
async def db_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with factory() as db:
        db.add(
            User(
                username=TEST_ADMIN["username"],
                password_hash=hash_password(TEST_ADMIN["password"]),
                role="admin",
                is_active=True,
            )
        )
        db.add(Cluster(name="default", description="Default cluster"))
        await seed_catalog(db)
        await db.commit()

    yield factory
    await engine.dispose()


@pytest.fixture
async def db(db_factory) -> AsyncIterator[AsyncSession]:
    async with db_factory() as session:
        yield session


@pytest.fixture
async def client(db_factory) -> AsyncIterator[AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def add_connected_worker(
    db: AsyncSession,
    cluster_id,
    name: str = "w1",
    gpus: tuple[tuple[int, str, str, int], ...] = ((0, "mock", "Mock GPU A", 24576),),
) -> Worker:
    worker = Worker(
        cluster_id=cluster_id,
        name=name,
        hostname=f"{name}-host",
        state="connected",
        token_hash=sha256_hex("tok"),
    )
    db.add(worker)
    await db.flush()
    for index, vendor, gpu_name, vram_mb in gpus:
        db.add(
            WorkerGPU(
                worker_id=worker.id,
                index=index,
                vendor=vendor,
                name=gpu_name,
                vram_mb=vram_mb,
                used_vram_mb=0,
                utilization_pct=0,
            )
        )
    await db.commit()
    await db.refresh(worker)
    return worker
