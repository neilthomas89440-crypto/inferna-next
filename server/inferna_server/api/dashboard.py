"""Dashboard aggregate endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import Cluster, ModelInstance, User, Worker, WorkerGPU
from inferna_server.schemas import DashboardOut, InstanceOut

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardOut:
    clusters = (await db.execute(select(func.count()).select_from(Cluster))).scalar_one()
    workers_online = (
        await db.execute(
            select(func.count()).select_from(Worker).where(Worker.state == "connected")
        )
    ).scalar_one()
    gpus_total = (await db.execute(select(func.count()).select_from(WorkerGPU))).scalar_one()
    vram_total_mb = (
        await db.execute(select(func.coalesce(func.sum(WorkerGPU.vram_mb), 0)))
    ).scalar_one()
    vram_used_mb = (
        await db.execute(select(func.coalesce(func.sum(WorkerGPU.used_vram_mb), 0)))
    ).scalar_one()
    instances_running = (
        await db.execute(
            select(func.count()).select_from(ModelInstance).where(ModelInstance.state == "running")
        )
    ).scalar_one()

    rows = await db.execute(
        select(ModelInstance)
        .options(selectinload(ModelInstance.model), selectinload(ModelInstance.worker))
        .order_by(ModelInstance.created_at.desc())
        .limit(10)
    )
    recent: list[InstanceOut] = []
    for inst in rows.scalars().all():
        inst.worker_name = inst.worker.name if inst.worker else None
        recent.append(InstanceOut.model_validate(inst))

    return DashboardOut(
        clusters=clusters,
        workers_online=workers_online,
        gpus_total=gpus_total,
        vram_used_mb=vram_used_mb,
        vram_total_mb=vram_total_mb,
        instances_running=instances_running,
        instances=recent,
    )
