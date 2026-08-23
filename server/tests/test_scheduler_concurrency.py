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

    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            await seed_catalog(db)
            cluster = (
                await db.execute(select(Cluster).where(Cluster.name == "default"))
            ).scalar_one_or_none()
            if cluster is None:
                cluster = Cluster(name="default", description="Default cluster")
                db.add(cluster)
                await db.commit()
            from inferna_server.services.workers_svc import sha256_hex

            # Re-runs on the same DB: clear workers left by a previous run (GPUs cascade).
            for w in (
                await db.execute(select(Worker).where(Worker.hostname == "conc-host"))
            ).scalars().all():
                await db.delete(w)
            await db.commit()

            # Single GPU with 4096 MB — fits exactly 2×2048 MB models
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
                    vram_mb=4096,
                    used_vram_mb=0,
                    utilization_pct=0,
                )
            )
            await db.commit()
            await db.refresh(worker)

            model = (await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-0.5B-Instruct"))).scalar_one()
            assert model.vram_required_mb == 2048

            # 5 concurrent allocate+create — only 2 should succeed, 3 should get 400
            import asyncio

            from fastapi import HTTPException

            from inferna_server.models import ModelInstance

            async def try_allocate():
                async with Session() as s:
                    try:
                        w, gpu_indexes, port = await allocate_auto(s, cluster.id, model.vram_required_mb, "vllm")
                        inst = ModelInstance(
                            model_id=model.id,
                            cluster_id=cluster.id,
                            worker_id=w.id,
                            engine="vllm",
                            profile="latency",
                            gpu_indexes=gpu_indexes,
                            state="scheduled",
                            desired_state="running",
                            generation=1,
                            port=port,
                        )
                        s.add(inst)
                        await s.commit()
                        return True, port
                    except HTTPException as exc:
                        await s.rollback()
                        # expected for overflow
                        if exc.status_code == 400 and "no GPU with enough free VRAM" in exc.detail:
                            return False, None
                        # port conflict also acceptable as 409
                        if exc.status_code == 409:
                            return False, None
                        raise
                    except Exception:
                        await s.rollback()
                        raise

            results = await asyncio.gather(*[try_allocate() for _ in range(5)])
            successes = [r for r in results if r[0]]
            failures = [r for r in results if not r[0]]
            assert len(successes) == 2, f"expected 2 successes, got {len(successes)}: {results}"
            assert len(failures) == 3, f"expected 3 failures, got {len(failures)}: {results}"
            # also ensure ports are unique among successes
            ports = [p for _, p in successes]
            assert len(set(ports)) == len(ports)
    finally:
        await engine.dispose()
