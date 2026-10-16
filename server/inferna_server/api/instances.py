"""Model instance lifecycle: deploy, list, stop, delete (any authenticated user)."""

from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import (
    Cluster,
    Deployment,
    Model,
    ModelInstance,
    User,
    Worker,
    as_aware_utc,
)
from inferna_server.schemas import DeployRequest, InstanceLogsOut, InstanceOut, ManualGpuSelection
from inferna_server.services.logs import tail_lines
from inferna_server.services.scheduler import (
    allocate_manual,
    allocate_parallel_replicas,
    allocate_replicas,
    rendezvous_host,
)

# The worker polls every ~5 s, so a bounded wait of a few intervals is what "live" can mean in a
# pull-based control plane.
LIVE_LOG_WAIT_SECONDS = 12.0
LIVE_LOG_POLL_SECONDS = 0.5
LIVE_LOG_DEFAULT_LINES = 100
LIVE_LOG_MAX_LINES = 1000

router = APIRouter(prefix="/model-instances", tags=["instances"])


def _with_names(instances: list[ModelInstance]) -> list[ModelInstance]:
    for inst in instances:
        inst.worker_name = inst.worker.name if inst.worker else None
    return instances


@router.get("", response_model=list[InstanceOut])
async def list_instances(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ModelInstance]:
    rows = await db.execute(
        select(ModelInstance)
        .options(selectinload(ModelInstance.model), selectinload(ModelInstance.worker))
        .order_by(ModelInstance.created_at.desc())
    )
    return _with_names(list(rows.scalars().all()))


@router.post("", response_model=list[InstanceOut], status_code=status.HTTP_201_CREATED)
async def deploy(
    body: DeployRequest,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ModelInstance]:
    model = await db.get(Model, body.model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    cluster = await db.get(Cluster, body.cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    if body.engine not in (model.supported_engines or []):
        raise HTTPException(
            status_code=400,
            detail=f"engine '{body.engine}' does not support category '{model.category}'",
        )
    multi = body.nodes_per_replica > 1
    if isinstance(body.gpu_selection, ManualGpuSelection) and (body.replicas != 1 or multi):
        raise HTTPException(
            status_code=400, detail="manual gpu selection supports a single replica"
        )
    if multi and body.parallel_mode == "single":
        raise HTTPException(
            status_code=400,
            detail="parallel_mode must be dp, pp, tp or pd for a multi-node deploy",
        )
    if not multi and body.parallel_mode != "single":
        raise HTTPException(status_code=400, detail="parallel_mode requires nodes_per_replica > 1")
    if multi and body.replicas != 1:
        raise HTTPException(
            status_code=400,
            detail="multi-node deploy supports one replica group for now",
        )

    try:
        deployment = Deployment(
            model_id=model.id,
            cluster_id=cluster.id,
            engine=body.engine,
            profile=body.profile,
            min_replicas=body.replicas,
            max_replicas=body.replicas,
            nodes_per_replica=body.nodes_per_replica,
            parallel_mode=body.parallel_mode,
        )
        instances: list[ModelInstance] = []
        if multi:
            # One parallel group: `nodes_per_replica` ranks, one host each, head first.
            allocations = await allocate_parallel_replicas(
                db, cluster.id, model.vram_required_mb, body.engine, body.nodes_per_replica
            )
            head = allocations[0]
            head_host = rendezvous_host(head.worker.address, head.worker.hostname)
            rendezvous = f"{head_host}:{head.rendezvous_port}"
            group_id = uuid.uuid4()
            for allocation in allocations:
                instance = ModelInstance(
                    model_id=model.id,
                    cluster_id=cluster.id,
                    worker_id=allocation.worker.id,
                    engine=body.engine,
                    profile=body.profile,
                    gpu_indexes=allocation.gpu_indexes,
                    state="scheduled",
                    desired_state="running",
                    generation=1,
                    # Followers publish no port: the gateway must only ever reach the head.
                    port=allocation.instance_port,
                    deployment=deployment,
                    parallel_group_id=group_id,
                    node_rank=allocation.node_rank,
                    world_size=body.nodes_per_replica,
                    parallel_mode=body.parallel_mode,
                    rendezvous=rendezvous,
                )
                instance.model = model
                instance.worker = allocation.worker
                instance.worker_name = allocation.worker.name
                instances.append(instance)
        else:
            if isinstance(body.gpu_selection, ManualGpuSelection):
                worker, gpu_indexes, port = await allocate_manual(
                    db,
                    body.cluster_id,
                    body.gpu_selection.worker_id,
                    body.gpu_selection.gpu_indexes,
                    model.vram_required_mb,
                    body.engine,
                )
                allocations_single = [(worker, gpu_indexes, port)]
            else:
                allocations_single = await allocate_replicas(
                    db, cluster.id, model.vram_required_mb, body.engine, body.replicas, used=set()
                )
            for worker, gpu_indexes, port in allocations_single:
                instance = ModelInstance(
                    model_id=model.id,
                    cluster_id=cluster.id,
                    worker_id=worker.id,
                    engine=body.engine,
                    profile=body.profile,
                    gpu_indexes=gpu_indexes,
                    state="scheduled",
                    desired_state="running",
                    generation=1,
                    port=port,
                    deployment=deployment,
                )
                instance.model = model
                instance.worker = worker
                instance.worker_name = worker.name
                instances.append(instance)
        db.add(deployment)
        db.add_all(instances)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="allocation conflict; retry") from None
    return _with_names(instances)
@router.post("/{instance_id}/stop", response_model=InstanceOut)
async def stop_instance(
    instance_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelInstance:
    instance = (
        await db.execute(
            select(ModelInstance)
            .options(
                selectinload(ModelInstance.worker),
                selectinload(ModelInstance.model),
            )
            .where(ModelInstance.id == instance_id)
        )
    ).scalar_one_or_none()
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    if instance.desired_state != "stopped":
        instance.desired_state = "stopped"
        instance.generation += 1
        instance.error_detail = None
        await db.commit()
        await db.refresh(instance)
    # Populate worker_name for response even on no-op (already stopped) — best-effort
    if instance.worker is not None:
        instance.worker_name = instance.worker.name
    return instance


@router.post("/{instance_id}/restart", response_model=InstanceOut)
async def restart_instance(
    instance_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelInstance:
    instance = (
        await db.execute(
            select(ModelInstance)
            .options(
                selectinload(ModelInstance.worker),
                selectinload(ModelInstance.model),
            )
            .where(ModelInstance.id == instance_id)
        )
    ).scalar_one_or_none()
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    instance.desired_state = "running"
    instance.generation += 1
    instance.error_detail = None
    await db.commit()
    await db.refresh(instance)
    if instance.worker is not None:
        instance.worker_name = instance.worker.name
    return instance


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instance(
    instance_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    instance = (
        await db.execute(
            select(ModelInstance)
            .options(selectinload(ModelInstance.deployment).selectinload(Deployment.instances))
            .where(ModelInstance.id == instance_id)
        )
    ).scalar_one_or_none()
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    # Deleting the last replica removes the group too: a deployment always has >=1 instance.
    if instance.deployment is not None and len(instance.deployment.instances) <= 1:
        await db.delete(instance.deployment)
    else:
        await db.delete(instance)
    await db.commit()


@router.get("/{instance_id}/logs", response_model=InstanceLogsOut)
async def instance_logs(
    instance_id: uuid.UUID,
    live: bool = Query(default=False, description="wait for the worker's next Sync first"),
    lines: int = Query(default=LIVE_LOG_DEFAULT_LINES, ge=1, le=LIVE_LOG_MAX_LINES),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InstanceLogsOut:
    """The worker's last log snapshot for this instance.

    With `live=true` the endpoint waits (bounded) for the next Sync, because the control plane is
    pull-based and the server never dials a GPU host — a follower rank needs no inbound access
    either (see docs/network-matrix.md). `stale=true` means the wait expired and the answer is the
    previous snapshot rather than a failure: a five-second-old log tail is still useful.
    """
    instance = (
        await db.execute(select(ModelInstance).where(ModelInstance.id == instance_id))
    ).scalar_one_or_none()
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")

    # Freshness is measured on the *worker's* heartbeat, not on `updated_at`: a Sync that changes
    # nothing legitimately leaves the row untouched, so "has the worker reported since you asked?"
    # is the honest question. The tail itself is written by that Sync when it changed.
    stale = False
    if live:
        worker = (
            await db.execute(select(Worker).where(Worker.id == instance.worker_id))
        ).scalar_one_or_none()
        if worker is None:
            # An instance without a registered worker cannot be refreshed: say so rather than
            # pretending the snapshot is live.
            stale = True
        else:
            seen_before = as_aware_utc(worker.last_seen_at) if worker.last_seen_at else None
            deadline = time.monotonic() + LIVE_LOG_WAIT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(LIVE_LOG_POLL_SECONDS)
                await db.refresh(worker)
                seen_now = as_aware_utc(worker.last_seen_at) if worker.last_seen_at else None
                if seen_now is not None and (seen_before is None or seen_now > seen_before):
                    break
            else:
                stale = True
        await db.refresh(instance)

    return InstanceLogsOut(
        instance_id=instance.id,
        state=instance.state,
        log_tail=tail_lines(instance.log_tail, lines),
        updated_at=instance.updated_at,
        stale=stale,
        lines=lines,
    )
