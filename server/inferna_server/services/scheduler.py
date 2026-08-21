"""Scheduler: GPU best-fit allocation + per-worker host port allocation."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.config import get_settings
from inferna_server.models import LIVE_STATES, ModelInstance, Worker
from inferna_server.services.compatibility import ENGINE_VENDORS

PORT_RESERVED = or_(
    ModelInstance.desired_state == "running",
    ModelInstance.state.in_(LIVE_STATES),
)


async def _live_instances(db: AsyncSession, worker_id: uuid.UUID) -> list[ModelInstance]:
    rows = await db.execute(
        select(ModelInstance)
        .options(selectinload(ModelInstance.model))
        .where(ModelInstance.worker_id == worker_id, ModelInstance.state.in_(LIVE_STATES))
    )
    return list(rows.scalars().all())


async def _vram_instances(db: AsyncSession, worker_id: uuid.UUID) -> list[ModelInstance]:
    rows = await db.execute(
        select(ModelInstance)
        .options(selectinload(ModelInstance.model))
        .where(ModelInstance.worker_id == worker_id, ModelInstance.state.in_(LIVE_STATES))
    )
    return list(rows.scalars().all())


async def _port_instances(db: AsyncSession, worker_id: uuid.UUID) -> list[ModelInstance]:
    rows = await db.execute(
        select(ModelInstance).where(
            ModelInstance.worker_id == worker_id,
            PORT_RESERVED,
        )
    )
    return list(rows.scalars().all())


def _gpu_usage(instances: list[ModelInstance]) -> dict[int, int]:
    usage: dict[int, int] = {}
    for inst in instances:
        for index in inst.gpu_indexes:
            usage[index] = usage.get(index, 0) + inst.model.vram_required_mb
    return usage


def _alloc_port(instances: list[ModelInstance]) -> int:
    used = {i.port for i in instances if i.port}
    for port in get_settings().instance_port_range:
        if port not in used:
            return port
    raise HTTPException(status_code=400, detail="no free host port in instance port range")


def _connected_workers(db: AsyncSession, cluster_id: uuid.UUID):
    return db.execute(
        select(Worker)
        .options(selectinload(Worker.gpus))
        .where(Worker.cluster_id == cluster_id, Worker.state == "connected")
        .order_by(Worker.name)
        .with_for_update()
    )


async def allocate_auto(
    db: AsyncSession, cluster_id: uuid.UUID, vram_required_mb: int, engine: str
) -> tuple[Worker, list[int], int]:
    """Pick the connected worker/GPU with the smallest free VRAM that fits (best-fit)."""
    workers = (await _connected_workers(db, cluster_id)).scalars().all()
    if not workers:
        raise HTTPException(status_code=400, detail="no connected workers in cluster")

    best: tuple[int, Worker, int] | None = None  # (free_mb, worker, gpu_index)
    for worker in workers:
        usage = _gpu_usage(await _vram_instances(db, worker.id))
        for gpu in worker.gpus:
            if gpu.vendor not in ENGINE_VENDORS.get(engine, set()):
                continue
            free = gpu.vram_mb - usage.get(gpu.index, 0)
            if free >= vram_required_mb and (best is None or free < best[0]):
                best = (free, worker, gpu.index)

    if best is None:
        raise HTTPException(status_code=400, detail="no GPU with enough free VRAM in cluster")
    _, worker, gpu_index = best
    port = _alloc_port(await _port_instances(db, worker.id))
    return worker, [gpu_index], port


async def allocate_manual(
    db: AsyncSession,
    cluster_id: uuid.UUID,
    worker_id: uuid.UUID,
    gpu_indexes: list[int],
    vram_required_mb: int,
    engine: str,
) -> tuple[Worker, list[int], int]:
    worker = (
        await db.execute(
            select(Worker)
            .options(selectinload(Worker.gpus))
            .where(Worker.id == worker_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if worker is None or worker.state != "connected":
        raise HTTPException(status_code=400, detail="worker not found or not connected")
    if worker.cluster_id != cluster_id:
        raise HTTPException(status_code=400, detail="worker does not belong to cluster")

    available = {g.index for g in worker.gpus}
    for index in gpu_indexes:
        if index not in available:
            raise HTTPException(status_code=400, detail=f"worker has no GPU index {index}")

    gpus_by_index = {g.index: g for g in worker.gpus}
    for index in gpu_indexes:
        if gpus_by_index[index].vendor not in ENGINE_VENDORS.get(engine, set()):
            raise HTTPException(
                status_code=400, detail=f"engine {engine} not supported on {gpus_by_index[index].vendor} GPU"
            )

    usage = _gpu_usage(await _vram_instances(db, worker.id))
    for index in gpu_indexes:
        gpu = gpus_by_index[index]
        if gpu.vram_mb - usage.get(index, 0) < vram_required_mb:
            raise HTTPException(
                status_code=400, detail=f"GPU {index} does not fit {vram_required_mb} MB"
            )

    port = _alloc_port(await _port_instances(db, worker.id))
    return worker, sorted(set(gpu_indexes)), port
