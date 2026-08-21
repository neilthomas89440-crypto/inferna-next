"""Concurrent allocation serialization test (PostgreSQL only)."""

import pytest
from sqlalchemy import select

from inferna_server.config import get_settings
from inferna_server.models import Base, Cluster, Model, Worker, WorkerGPU
from inferna_server.services.scheduler import allocate_auto


@pytest.mark.asyncio
async def test_concurrent_vram_allocation_serialized():
    if not get_settings().database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL required for concurrency test")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from inferna_server.services.workers_svc import seed_catalog

    # Build own PG engine/sessionmaker
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            await seed_catalog(db)
            await db.commit()
            cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
            # single GPU worker with limited VRAM
            from inferna_server.services.workers_svc import sha256_hex

            worker = Worker(
                cluster_id=cluster.id,
                name="conc-worker",
                hostname="conc-host",
                state="connected",
                token_hash=sha256_hex("tok"),
            )
            db.add(worker)
            await db.flush()
            db.add(
                WorkerGPU(
                    worker_id=worker.id,
                    index=0,
                    vendor="nvidia",
                    name="A100",
                    vram_mb=16384,
                    used_vram_mb=0,
                    utilization_pct=0,
                )
            )
            await db.commit()
            await db.refresh(worker)

            model = (await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-0.5B-Instruct"))).scalar_one()

            # Try two concurrent allocations that together would exceed VRAM
            # Model needs 2048, GPU has 16384, so 10 concurrent could fit, but we test serialization not to duplicate port.
            # Run two allocates concurrently in separate sessions to check advisory lock / unique index handles it.
            async def alloc():
                async with Session() as s:
                    w, idx, port = await allocate_auto(s, cluster.id, model.vram_required_mb, "vllm")
                    # do not commit instance, just return port
                    return port

            import asyncio

            ports = await asyncio.gather(alloc(), alloc())
            assert ports[0] != ports[1], "concurrent allocates should get different ports"
    finally:
        await engine.dispose()
