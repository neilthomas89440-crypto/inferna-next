"""Cluster management: list (any user), create/delete (admin)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.auth import get_current_user, require_admin
from inferna_server.db import get_db
from inferna_server.models import LIVE_STATES, Cluster, ModelInstance, User, Worker
from inferna_server.schemas import ClusterCreate, ClusterOut

router = APIRouter(prefix="/clusters", tags=["clusters"])


@router.get("", response_model=list[ClusterOut])
async def list_clusters(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Cluster]:
    rows = await db.execute(select(Cluster).order_by(Cluster.name))
    return list(rows.scalars().all())


@router.post("", response_model=ClusterOut, status_code=status.HTTP_201_CREATED)
async def create_cluster(
    body: ClusterCreate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Cluster:
    existing = (
        await db.execute(select(Cluster).where(Cluster.name == body.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="cluster name already taken")
    cluster = Cluster(name=body.name, description=body.description)
    db.add(cluster)
    await db.commit()
    await db.refresh(cluster)
    return cluster


@router.delete("/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cluster(
    cluster_id: uuid.UUID,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    workers = (
        await db.execute(
            select(func.count()).select_from(Worker).where(Worker.cluster_id == cluster_id)
        )
    ).scalar_one()
    if workers > 0:
        raise HTTPException(status_code=400, detail="cluster has workers")
    live = (
        await db.execute(
            select(func.count())
            .select_from(ModelInstance)
            .where(ModelInstance.cluster_id == cluster_id, ModelInstance.state.in_(LIVE_STATES))
        )
    ).scalar_one()
    if live > 0:
        raise HTTPException(status_code=400, detail="cluster has live instances")
    await db.delete(cluster)
    await db.commit()
