"""Worker endpoints: list (any user), remove (admin)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.auth import get_current_user, require_admin
from inferna_server.db import get_db
from inferna_server.models import LIVE_STATES, ModelInstance, User, Worker
from inferna_server.schemas import WorkerOut

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=list[WorkerOut])
async def list_workers(
    cluster_id: uuid.UUID | None = None,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Worker]:
    query = (
        select(Worker)
        .options(
            selectinload(Worker.gpus),
            selectinload(Worker.instances).selectinload(ModelInstance.model),
        )
        .order_by(Worker.name)
    )
    if cluster_id is not None:
        query = query.where(Worker.cluster_id == cluster_id)
    workers = list((await db.execute(query)).scalars().all())
    for worker in workers:
        for inst in worker.instances:
            inst.worker_name = worker.name
    return workers


@router.delete("/{worker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_worker(
    worker_id: uuid.UUID,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    worker = await db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    # Mark its live instances as errored and detach them before removing the row.
    live = (
        (
            await db.execute(
                select(ModelInstance).where(
                    ModelInstance.worker_id == worker_id,
                    ModelInstance.state.in_(LIVE_STATES),
                )
            )
        )
        .scalars()
        .all()
    )
    for inst in live:
        inst.worker_id = None
        inst.state = "error"
        inst.error_detail = "worker removed"
    await db.delete(worker)
    await db.commit()
