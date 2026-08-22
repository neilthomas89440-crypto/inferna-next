"""Scheduler tests: best-fit GPU selection, manual validation, port allocation."""

from __future__ import annotations

import pytest
from conftest import add_connected_worker
from fastapi import HTTPException
from sqlalchemy import select

from inferna_server.models import Cluster, Model, ModelInstance
from inferna_server.services.scheduler import allocate_auto, allocate_manual


async def _default_cluster(db) -> Cluster:
    return (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()


async def _model(db, name: str) -> Model:
    return (await db.execute(select(Model).where(Model.name == name))).scalar_one()


async def _deploy(db, worker_id, model_id, gpu_indexes, port) -> ModelInstance:
    instance = ModelInstance(
        model_id=model_id,
        cluster_id=(await db.execute(select(Cluster).where(Cluster.name == "default")))
        .scalar_one()
        .id,
        worker_id=worker_id,
        engine="vllm",
        profile="latency",
        gpu_indexes=gpu_indexes,
        state="scheduled",
        desired_state="running",
        generation=1,
        port=port,
    )
    db.add(instance)
    await db.commit()
    return instance


async def test_best_fit_picks_smallest_fitting_gpu(db) -> None:
    """Two GPUs (24 GB, 48 GB); a 16 GB model must land on the 24 GB GPU."""
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(
        db,
        cluster.id,
        name="fit-worker",
        gpus=((0, "mock", "A", 24576), (1, "mock", "B", 49152)),
    )
    model = await _model(db, "Qwen/Qwen2.5-7B-Instruct")
    chosen_worker, indexes, port = await allocate_auto(db, cluster.id, model.vram_required_mb, "vllm")
    assert chosen_worker.id == worker.id
    assert indexes == [0]
    assert port == 8010

async def test_second_instance_uses_next_gpu(db) -> None:
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(
        db,
        cluster.id,
        name="two-gpu",
        gpus=((0, "mock", "A", 24576), (1, "mock", "B", 49152)),
    )
    model = await _model(db, "Qwen/Qwen2.5-7B-Instruct")
    await _deploy(db, worker.id, model.id, [0], 8010)
    # GPU 0 now has 24-16=8 GB free; GPU 1 free 48 GB -> picked.
    _, indexes, port = await allocate_auto(db, cluster.id, model.vram_required_mb, "vllm")
    assert indexes == [1]
    assert port == 8011

async def test_no_fit_returns_400(db) -> None:
    cluster = await _default_cluster(db)
    await add_connected_worker(db, cluster.id, name="small", gpus=((0, "mock", "A", 8192),))
    with pytest.raises(HTTPException) as exc:
        await allocate_auto(db, cluster.id, 16384, "vllm")
    assert exc.value.status_code == 400
    assert "no GPU with enough free VRAM" in exc.value.detail


async def test_no_connected_workers_returns_400(db) -> None:
    cluster = await _default_cluster(db)
    with pytest.raises(HTTPException) as exc:
        await allocate_auto(db, cluster.id, 2048, "vllm")
    assert exc.value.status_code == 400


async def test_manual_wrong_gpu_rejected(db) -> None:
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(db, cluster.id, name="m", gpus=((0, "mock", "A", 24576),))
    with pytest.raises(HTTPException) as exc:
        await allocate_manual(db, cluster.id, worker.id, [7], 2048, "vllm")
    assert exc.value.status_code == 400
    assert "no GPU index 7" in exc.value.detail


async def test_manual_disconnected_worker_rejected(db) -> None:
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(db, cluster.id, name="gone")
    worker.state = "disconnected"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await allocate_manual(db, cluster.id, worker.id, [0], 2048, "vllm")
    assert exc.value.status_code == 400


async def test_manual_gpu_too_small_rejected(db) -> None:
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(db, cluster.id, name="tiny", gpus=((0, "mock", "A", 4096),))
    with pytest.raises(HTTPException) as exc:
        await allocate_manual(db, cluster.id, worker.id, [0], 16384, "vllm")
    assert exc.value.status_code == 400
    assert "does not fit" in exc.value.detail


async def test_manual_ok(db) -> None:
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(
        db, cluster.id, name="manual", gpus=((1, "mock", "B", 24576),)
    )
    chosen, indexes, port = await allocate_manual(db, cluster.id, worker.id, [1], 2048, "vllm")
    assert chosen.id == worker.id
    assert indexes == [1]
    assert port == 8010
async def test_port_pool_exhausted_returns_400(db) -> None:
    cluster = await _default_cluster(db)
    worker = await add_connected_worker(db, cluster.id, name="ports")
    model = await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")
    # Occupy every port without consuming VRAM (empty gpu_indexes).
    for port in range(8010, 8101):
        await _deploy(db, worker.id, model.id, [], port)
    with pytest.raises(HTTPException) as exc:
        await allocate_auto(db, cluster.id, model.vram_required_mb, "vllm")
    assert exc.value.status_code == 400
    assert "no free host port" in exc.value.detail


async def test_manual_cross_cluster_rejected(db) -> None:
    cluster_a = await _default_cluster(db)
    # create second cluster
    other = Cluster(name="other")
    db.add(other)
    await db.commit()
    worker = await add_connected_worker(db, cluster_a.id, name="cross")
    with pytest.raises(HTTPException) as exc:
        await allocate_manual(db, other.id, worker.id, [0], 2048, "vllm")
    assert exc.value.status_code == 400
    assert "worker does not belong to cluster" in exc.value.detail


async def test_manual_vendor_mismatch_rejected(db) -> None:
    cluster = await _default_cluster(db)
    # create worker with amd GPU
    worker = await add_connected_worker(
        db, cluster.id, name="amd-worker", gpus=((0, "amd", "AMD GPU", 24576),)
    )
    with pytest.raises(HTTPException) as exc:
        await allocate_manual(db, cluster.id, worker.id, [0], 2048, "vllm")
    assert exc.value.status_code == 400
    assert "not supported on amd" in exc.value.detail


async def test_port_unique_constraint(db) -> None:
    from sqlalchemy.exc import IntegrityError

    cluster = await _default_cluster(db)
    worker = await add_connected_worker(db, cluster.id, name="uniq")
    model = await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")
    # Two instances with same worker_id+port should violate partial unique index
    inst1 = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        worker_id=worker.id,
        engine="vllm",
        profile="latency",
        gpu_indexes=[0],
        state="scheduled",
        desired_state="running",
        generation=1,
        port=8010,
    )
    inst2 = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        worker_id=worker.id,
        engine="vllm",
        profile="latency",
        gpu_indexes=[0],
        state="scheduled",
        desired_state="running",
        generation=1,
        port=8010,
    )
    db.add(inst1)
    await db.commit()
    db.add(inst2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_sequential_deploy_different_ports(client, db) -> None:
    # Use API level to check sequential deploys get different ports
    from tests.conftest import TEST_ADMIN, auth_headers, login

    # Ensure worker exists
    cluster = await _default_cluster(db)
    await add_connected_worker(db, cluster.id, name="seq")
    model = await _model(db, "Qwen/Qwen2.5-0.5B-Instruct")
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    headers = auth_headers(token)
    body = {
        "model_id": str(model.id),
        "cluster_id": str(cluster.id),
        "engine": "vllm",
        "profile": "latency",
        "gpu_selection": "auto",
    }
    resp1 = await client.post("/api/v1/model-instances", json=body, headers=headers)
    assert resp1.status_code == 201
    resp2 = await client.post("/api/v1/model-instances", json=body, headers=headers)
    assert resp2.status_code == 201
    assert resp1.json()["port"] != resp2.json()["port"]
