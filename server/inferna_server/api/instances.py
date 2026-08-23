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
from inferna_server.models import Cluster, Model, ModelInstance, User
from inferna_server.schemas import DeployRequest, InstanceOut, ManualGpuSelection
from inferna_server.services.scheduler import allocate_auto, allocate_manual

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


@router.post("", response_model=InstanceOut, status_code=status.HTTP_201_CREATED)
async def deploy(
    body: DeployRequest,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelInstance:
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
        else:
            worker, gpu_indexes, port = await allocate_auto(
                db, body.cluster_id, model.vram_required_mb, body.engine
            )
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
        )
        instance.model = model
        instance.worker = worker
        instance.worker_name = worker.name
        db.add(instance)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="allocation conflict; retry") from None
    return instance
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
    instance = await db.get(ModelInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    await db.delete(instance)
    await db.commit()
