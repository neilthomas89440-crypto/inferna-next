"""Model instance lifecycle: deploy, list, stop, delete (any authenticated user)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import Cluster, Deployment, Model, ModelInstance, User
from inferna_server.schemas import DeployRequest, InstanceOut, ManualGpuSelection
from inferna_server.services.scheduler import allocate_manual, allocate_replicas

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
    if isinstance(body.gpu_selection, ManualGpuSelection) and body.replicas != 1:
        raise HTTPException(
            status_code=400, detail="manual gpu selection supports a single replica"
        )

    try:
        if isinstance(body.gpu_selection, ManualGpuSelection):
            worker, gpu_indexes, port = await allocate_manual(
                db,
                body.cluster_id,
                body.gpu_selection.worker_id,
                body.gpu_selection.gpu_indexes,
                model.vram_required_mb,
                body.engine,
            )
            allocations = [(worker, gpu_indexes, port)]
        else:
            allocations = await allocate_replicas(
                db, cluster.id, model.vram_required_mb, body.engine, body.replicas, used=set()
            )
        deployment = Deployment(
            model_id=model.id,
            cluster_id=cluster.id,
            engine=body.engine,
            profile=body.profile,
            min_replicas=body.replicas,
            max_replicas=body.replicas,
        )
        instances: list[ModelInstance] = []
        for worker, gpu_indexes, port in allocations:
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
