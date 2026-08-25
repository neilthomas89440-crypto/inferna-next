"""Concurrent allocation serialization test (PostgreSQL only)."""

import pytest
from sqlalchemy import select

from inferna_server.config import get_settings
from inferna_server.models import (
    Base,
    Cluster,
    Deployment,
    Model,
    ModelInstance,
    Worker,
    WorkerGPU,
)
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
            # Re-runs on the same DB: clear workers left by previous runs of either
            # concurrency test (their deployments first — instance rows cascade with
            # them, worker_id only SET NULLs). Leftover connected workers would add
            # free VRAM to the cluster and break this test's capacity assumption.
            stale_workers = (
                await db.execute(
                    select(Worker).where(
                        Worker.hostname.in_(["conc-host"])
                        | Worker.hostname.like("scale-conc-host%")
                    )
                )
            ).scalars().all()
            if stale_workers:
                dep_ids = (
                    await db.execute(
                        select(ModelInstance.deployment_id.distinct()).where(
                            ModelInstance.worker_id.in_([w.id for w in stale_workers])
                        )
                    )
                ).scalars().all()
                for d in (
                    await db.execute(select(Deployment).where(Deployment.id.in_(dep_ids)))
                ).scalars().all():
                    await db.delete(d)
                for w in stale_workers:
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

            async def try_allocate():
                async with Session() as s:
                    try:
                        w, gpu_indexes, port = await allocate_auto(s, cluster.id, model.vram_required_mb, "vllm")
                        dep = Deployment(
                            model_id=model.id,
                            cluster_id=cluster.id,
                            engine="vllm",
                            profile="latency",
                        )
                        inst = ModelInstance(
                            model_id=model.id,
                            cluster_id=cluster.id,
                            worker_id=w.id,
                            deployment=dep,
                            engine="vllm",
                            profile="latency",
                            gpu_indexes=gpu_indexes,
                            state="scheduled",
                            desired_state="running",
                            generation=1,
                            port=port,
                        )
                        s.add_all([dep, inst])
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


@pytest.mark.asyncio
async def test_concurrent_manual_scale_race_serialized():
    """F4: N concurrent POST /deployments/{id}/scale on one group (real Postgres).

    Completion order is arbitrary, so after all commits the group must be pinned to
    whatever the LAST completed apply_scale wrote: min=max=replicas, exactly that many
    live replicas, ports unique per worker (PRODUCT_SPEC: the 8010–8100 pool is per
    worker host) and no duplicated (worker_id, gpu_index) among running instances.
    """
    if not get_settings().database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL required for concurrency test")

    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import selectinload

    from inferna_server.auth import hash_password
    from inferna_server.main import app
    from inferna_server.models import Deployment, ModelInstance, User
    from inferna_server.services.workers_svc import seed_catalog, sha256_hex

    engine = create_async_engine(get_settings().database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with Session() as db:
            # Admin user for API auth: lifespan seeding never runs under ASGITransport.
            admin = (
                await db.execute(select(User).where(User.username == "conc-scale-admin"))
            ).scalar_one_or_none()
            if admin is None:
                db.add(
                    User(
                        username="conc-scale-admin",
                        password_hash=hash_password("inferna"),
                        role="admin",
                        is_active=True,
                    )
                )
            await seed_catalog(db)
            cluster = (
                await db.execute(select(Cluster).where(Cluster.name == "default"))
            ).scalar_one_or_none()
            if cluster is None:
                cluster = Cluster(name="default", description="Default cluster")
                db.add(cluster)
            await db.commit()

            # Re-runs on the same DB: clear scale-test workers left by a previous run
            # (their deployments first — instance rows cascade with them, worker_id only SET NULLs).
            old_workers = (
                await db.execute(select(Worker).where(Worker.hostname.like("scale-conc-host%")))
            ).scalars().all()
            if old_workers:
                old_dep_ids = (
                    await db.execute(
                        select(ModelInstance.deployment_id.distinct()).where(
                            ModelInstance.worker_id.in_([w.id for w in old_workers])
                        )
                    )
                ).scalars().all()
                for d in (
                    await db.execute(select(Deployment).where(Deployment.id.in_(old_dep_ids)))
                ).scalars().all():
                    await db.delete(d)
                for w in old_workers:
                    await db.delete(w)
                await db.commit()

            # 5 workers x 2 GPUs x 8192 MB — capacity far above the worst case
            # (1+2+3+4+5 = 15 replicas x 2048 MB), so no allocation can fall through
            # to Pass 2 or fail for lack of VRAM; every request must return 200.
            for i in range(5):
                worker = Worker(
                    cluster_id=cluster.id,
                    name=f"scale-conc-worker-{i}",
                    hostname=f"scale-conc-host-{i}",
                    state="connected",
                    token_hash=sha256_hex(f"tok-{i}"),
                )
                db.add(worker)
                await db.flush()
                for g in range(2):
                    db.add(
                        WorkerGPU(
                            worker_id=worker.id,
                            index=g,
                            vendor="nvidia",
                            name="A100",
                            vram_mb=8192,
                            used_vram_mb=0,
                            utilization_pct=0,
                        )
                    )
            await db.commit()

            model = (
                await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-0.5B-Instruct"))
            ).scalar_one()
            assert model.vram_required_mb == 2048

            deployment = Deployment(
                model_id=model.id,
                cluster_id=cluster.id,
                engine="vllm",
                profile="latency",
                min_replicas=1,
                max_replicas=1,
            )
            db.add(deployment)
            await db.commit()
            dep_id = deployment.id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/auth/login", json={"username": "conc-scale-admin", "password": "inferna"}
            )
            assert resp.status_code == 200, resp.text
            headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

            async def scale(replicas: int) -> tuple[int, int, str]:
                r = await client.post(
                    f"/api/v1/deployments/{dep_id}/scale",
                    json={"replicas": replicas},
                    headers=headers,
                )
                return replicas, r.status_code, r.text

            results = await asyncio.gather(*[scale(n) for n in range(1, 6)])

        statuses = [status for _, status, _ in results]
        assert statuses == [200] * 5, f"expected all 200, got: {results}"

        async with Session() as db:
            deployment = (
                await db.execute(
                    select(Deployment)
                    .options(selectinload(Deployment.instances))
                    .where(Deployment.id == dep_id)
                )
            ).scalar_one()
            live = [i for i in deployment.instances if i.desired_state == "running"]
            assert deployment.min_replicas == deployment.max_replicas, (
                f"min/max desynchronized after concurrent scales: "
                f"{deployment.min_replicas}/{deployment.max_replicas}, results={results}"
            )
            assert len(live) == deployment.min_replicas, (
                f"expected {deployment.min_replicas} live replicas, got {len(live)}, "
                f"results={results}"
            )
            # Host ports are a per-worker pool (PRODUCT_SPEC), so uniqueness is
            # per (worker, port); different hosts may legally reuse the same port.
            port_pairs = [
                (i.worker_id, i.port) for i in deployment.instances if i.port is not None
            ]
            assert len(set(port_pairs)) == len(port_pairs), (
                f"duplicate (worker_id, port) in group: {port_pairs}"
            )
            pairs = [(i.worker_id, idx) for i in live for idx in (i.gpu_indexes or [])]
            assert len(set(pairs)) == len(pairs), (
                f"duplicate (worker_id, gpu_index) among running instances: {pairs}"
            )
    finally:
        await engine.dispose()
