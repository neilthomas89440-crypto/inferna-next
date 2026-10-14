"""Scheduler: GPU best-fit allocation + per-worker host port allocation."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.config import get_settings
from inferna_server.models import LIVE_STATES, ModelInstance, Worker, WorkerGPU
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


def _alloc_port(instances: list[ModelInstance], extra: set[int] | None = None) -> int:
    extra = extra or set()
    used = {i.port for i in instances if i.port} | extra
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
        usage = _gpu_usage(await _live_instances(db, worker.id))
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
                status_code=400,
                detail=f"engine {engine} not supported on {gpus_by_index[index].vendor} GPU",
            )

    usage = _gpu_usage(await _live_instances(db, worker.id))
    for index in gpu_indexes:
        gpu = gpus_by_index[index]
        if gpu.vram_mb - usage.get(index, 0) < vram_required_mb:
            raise HTTPException(
                status_code=400, detail=f"GPU {index} does not fit {vram_required_mb} MB"
            )

    port = _alloc_port(await _port_instances(db, worker.id))
    return worker, sorted(set(gpu_indexes)), port


async def allocate_replicas(
    db: AsyncSession,
    cluster_id: uuid.UUID,
    vram_required_mb: int,
    engine: str,
    count: int,
    used: set[tuple[uuid.UUID, int]] | None = None,
) -> list[tuple[Worker, list[int], int]]:
    """Allocate `count` replicas with anti-affinity: Pass 1a spreads across workers,
    Pass 1b across other GPUs of an already-used worker, Pass 2 falls back to any
    fitting GPU (the free-VRAM check keeps the placement valid)."""
    if used is None:
        used = set()
    used_workers = {worker_id for worker_id, _ in used}
    workers = list((await _connected_workers(db, cluster_id)).scalars().all())
    if not workers:
        raise HTTPException(status_code=400, detail="no connected workers in cluster")

    # Per-worker snapshots: live VRAM usage and GPUs that support this engine.
    usage_by_worker: dict[uuid.UUID, dict[int, int]] = {}
    gpus_by_worker: dict[uuid.UUID, list[WorkerGPU]] = {}
    supported = ENGINE_VENDORS.get(engine, set())
    for worker in workers:
        usage_by_worker[worker.id] = _gpu_usage(await _live_instances(db, worker.id))
        gpus_by_worker[worker.id] = [g for g in worker.gpus if g.vendor in supported]
    pending_vram: dict[tuple[uuid.UUID, int], int] = {}  # committed by this call so far
    call_ports: set[int] = set()  # all ports handed out by this call

    def _best(allowed) -> tuple[int, Worker, int] | None:
        best: tuple[int, Worker, int] | None = None  # (free_mb, worker, gpu_index)
        for worker in workers:
            for gpu in gpus_by_worker[worker.id]:
                if not allowed(worker, gpu):
                    continue
                free = (
                    gpu.vram_mb
                    - usage_by_worker[worker.id].get(gpu.index, 0)
                    - pending_vram.get((worker.id, gpu.index), 0)
                )
                if free >= vram_required_mb and (best is None or free < best[0]):
                    best = (free, worker, gpu.index)
        return best

    allocations: list[tuple[Worker, list[int], int]] = []
    for i in range(1, count + 1):
        best = _best(lambda w, _: w.id not in used_workers)  # Pass 1a: fresh worker
        if best is None:
            best = _best(lambda w, g: (w.id, g.index) not in used)  # Pass 1b: fresh GPU
        if best is None:
            best = _best(lambda _w, _g: True)  # Pass 2: any fitting GPU
        if best is None:
            raise HTTPException(
                status_code=400,
                detail=f"no GPU with enough free VRAM for replica {i} of {count}",
            )
        _, worker, gpu_index = best
        port = _alloc_port(await _port_instances(db, worker.id), extra=call_ports)
        call_ports.add(port)
        key = (worker.id, gpu_index)
        used.add(key)
        used_workers.add(worker.id)
        pending_vram[key] = pending_vram.get(key, 0) + vram_required_mb
        allocations.append((worker, [gpu_index], port))
    return allocations


@dataclass(slots=True)
class ParallelAllocation:
    """One rank of a parallel group (multi-node).

    `instance_port` and `rendezvous_port` are set on the head only: rank > 0 publishes no
    port — the gateway must never reach a follower directly — and dials the head instead.
    """

    worker: Worker
    gpu_indexes: list[int]
    node_rank: int
    instance_port: int | None = None
    rendezvous_port: int | None = None


def rendezvous_host(address: str | None, fallback: str) -> str:
    """Bare host for a rendezvous address.

    Workers report their address as a URL (`http://10.0.0.1`), but `MASTER_ADDR` must be a
    bare host: passing the scheme through produces `http://10.0.0.1:8110`, which no rank can
    dial. Strips scheme, path, and a port when the remainder is not a bare IPv6 literal.
    """
    raw = (address or "").strip() or fallback.strip()
    for prefix in ("http://", "https://"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if raw.startswith("["):  # [::1]:8110
        return raw[1:].split("]", 1)[0]
    host, _, port = raw.rpartition(":")
    if port.isdigit() and host and ":" not in host:
        return host
    return raw


def _alloc_rendezvous_port(
    instances: list[ModelInstance], extra: set[int] | None = None
) -> int:
    """A free rendezvous port on the head's host, kept clear of its instance ports too."""
    used = {i.port for i in instances if i.port} | (extra or set())
    for instance in instances:
        if instance.rendezvous and ":" in instance.rendezvous:
            _, _, port = instance.rendezvous.rpartition(":")
            if port.isdigit():
                used.add(int(port))
    for port in get_settings().rendezvous_port_range:
        if port not in used:
            return port
    raise HTTPException(status_code=400, detail="no free rendezvous port in range")


def _best_fitting_gpu(
    worker: Worker,
    supported: Collection[str],
    usage: dict[int, int],
    vram_required_mb: int,
) -> tuple[int, int] | None:
    """(free_mb, gpu_index) of the worker's largest fitting GPU of a supported vendor."""
    best: tuple[int, int] | None = None
    for gpu in worker.gpus:
        if gpu.vendor not in supported:
            continue
        free = gpu.vram_mb - usage.get(gpu.index, 0)
        if free >= vram_required_mb and (best is None or free > best[0]):
            best = (free, gpu.index)
    return best


async def allocate_parallel_replicas(
    db: AsyncSession,
    cluster_id: uuid.UUID,
    vram_required_mb: int,
    engine: str,
    nodes_per_replica: int,
) -> list[ParallelAllocation]:
    """Place one rank per host for a parallel group; the head is the rank every host can dial.

    One rank per host is deliberate: two ranks of a pipeline on the same box share a NIC and
    the same GPUs, which hides interconnect problems. The head must have a routable
    `address` (roadmap §3.2) because every other rank dials it, and only the head receives an
    instance port so traffic can only ever reach rank 0 (invariant §3.3).
    """
    workers = list((await _connected_workers(db, cluster_id)).scalars().all())
    if not workers:
        raise HTTPException(status_code=400, detail="no connected workers in cluster")

    supported = ENGINE_VENDORS.get(engine, set())
    fitting: list[tuple[int, Worker, int]] = []
    for worker in workers:
        usage = _gpu_usage(await _live_instances(db, worker.id))
        best = _best_fitting_gpu(worker, supported, usage, vram_required_mb)
        if best is not None:
            fitting.append((best[0], worker, best[1]))

    heads = [entry for entry in fitting if entry[1].address]
    if not heads:
        raise HTTPException(
            status_code=400,
            detail="no connected worker with a routable address for the rendezvous head",
        )
    if len(fitting) < nodes_per_replica:
        raise HTTPException(
            status_code=400,
            detail=(
                f"multi-node deploy needs {nodes_per_replica} hosts with "
                f"{vram_required_mb} MB free, {len(fitting)} available"
            ),
        )

    def _order(entry: tuple[int, Worker, int]) -> tuple[int, str]:
        free_mb, worker, _ = entry
        return (-free_mb, worker.name)

    head = sorted(heads, key=_order)[0]
    others = sorted((e for e in fitting if e[1].id != head[1].id), key=_order)
    chosen = [head, *others[: nodes_per_replica - 1]]

    head_instances = await _port_instances(db, head[1].id)
    instance_port = _alloc_port(head_instances)
    rendezvous_port = _alloc_rendezvous_port(head_instances)

    return [
        ParallelAllocation(
            worker=worker,
            gpu_indexes=[gpu_index],
            node_rank=node_rank,
            instance_port=instance_port if node_rank == 0 else None,
            rendezvous_port=rendezvous_port if node_rank == 0 else None,
        )
        for node_rank, (_, worker, gpu_index) in enumerate(chosen)
    ]
