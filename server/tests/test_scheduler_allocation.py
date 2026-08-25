"""allocate_replicas tests: anti-affinity passes 1a/1b/2 and VRAM exhaustion."""

from __future__ import annotations

import pytest
from conftest import add_connected_worker
from fastapi import HTTPException
from sqlalchemy import select

from inferna_server.models import Cluster, Model
from inferna_server.services.scheduler import allocate_replicas


async def _default_cluster(db) -> Cluster:
    return (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()


async def _model(db, name: str) -> Model:
    return (await db.execute(select(Model).where(Model.name == name))).scalar_one()


async def test_pass_1a_spreads_across_workers(db) -> None:
    """Pass 1a: two workers with one GPU each — replicas land on different workers."""
    cluster = await _default_cluster(db)
    w1 = await add_connected_worker(db, cluster.id, name="affinity-a")
    w2 = await add_connected_worker(db, cluster.id, name="affinity-b")
    vram = (await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")).vram_required_mb

    allocations = await allocate_replicas(db, cluster.id, vram, "vllm", 2)

    assert [worker.id for worker, _, _ in allocations] == [w1.id, w2.id]
    ports = [port for _, _, port in allocations]
    assert len(set(ports)) == len(ports)


async def test_pass_1b_spreads_across_gpus_of_same_worker(db) -> None:
    """Pass 1b: a single worker with two GPUs — both GPUs get used before reuse."""
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(
        db, cluster.id, name="two-gpu", gpus=((0, "mock", "A", 24576), (1, "mock", "B", 24576))
    )
    vram = (await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")).vram_required_mb

    allocations = await allocate_replicas(db, cluster.id, vram, "vllm", 2)

    assert all(w.id == worker.id for w, _, _ in allocations)
    assert {gpu_indexes[0] for _, gpu_indexes, _ in allocations} == {0, 1}


async def test_spreads_across_gpus_then_reuses_best_fit(db) -> None:
    """One worker, two GPUs, three small replicas: both GPUs engaged, unique ports."""
    cluster = await _default_cluster(db)
    await add_connected_worker(
        db, cluster.id, name="spread", gpus=((0, "mock", "A", 24576), (1, "mock", "B", 49152))
    )
    vram = (await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")).vram_required_mb

    allocations = await allocate_replicas(db, cluster.id, vram, "vllm", 3)

    assert {gpu_indexes[0] for _, gpu_indexes, _ in allocations} == {0, 1}
    ports = [port for _, _, port in allocations]
    assert sorted(ports) == [8010, 8011, 8012]


async def test_pass_2_falls_back_to_same_gpu(db) -> None:
    """Pass 2: one big GPU and more replicas than GPUs — all share the same GPU."""
    cluster = await _default_cluster(db)
    await add_connected_worker(db, cluster.id, name="single-gpu")
    vram = (await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")).vram_required_mb

    allocations = await allocate_replicas(db, cluster.id, vram, "vllm", 3)

    assert all(gpu_indexes == [0] for _, gpu_indexes, _ in allocations)
    assert [port for _, _, port in allocations] == [8010, 8011, 8012]


async def test_exhausted_vram_returns_400_with_exact_detail(db) -> None:
    """A second 7B replica cannot share one 24 GB GPU — 400 names the failed replica."""
    cluster = await _default_cluster(db)
    await add_connected_worker(db, cluster.id, name="tight")
    vram = (await _model(db, "Qwen/Qwen2.5-7B-Instruct")).vram_required_mb

    with pytest.raises(HTTPException) as exc:
        await allocate_replicas(db, cluster.id, vram, "vllm", 2)

    assert exc.value.status_code == 400
    assert exc.value.detail == "no GPU with enough free VRAM for replica 2 of 2"
