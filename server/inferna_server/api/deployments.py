"""Deployment groups: list, scale, delete (any authenticated user)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.api.instances import _with_names
from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import Deployment, ModelInstance, User
from inferna_server.schemas import DeploymentOut, ScaleRequest
from inferna_server.services.scaling import apply_scale

router = APIRouter(prefix="/deployments", tags=["deployments"])

_DEPLOYMENT_LOADS = (
    selectinload(Deployment.model),
    selectinload(Deployment.instances).selectinload(ModelInstance.worker),
)


@router.get("", response_model=list[DeploymentOut])
async def list_deployments(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Deployment]:
    rows = await db.execute(
        select(Deployment).options(*_DEPLOYMENT_LOADS).order_by(Deployment.created_at.desc())
    )
    deployments = list(rows.scalars().all())
    for deployment in deployments:
        _with_names(list(deployment.instances))
    return deployments


@router.post("/{deployment_id}/scale", response_model=DeploymentOut)
async def scale_deployment(
    deployment_id: uuid.UUID,
    body: ScaleRequest,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Deployment:
    deployment = (
        await db.execute(
            select(Deployment).options(*_DEPLOYMENT_LOADS).where(Deployment.id == deployment_id)
        )
    ).scalar_one_or_none()
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    try:
        await apply_scale(db, deployment, body.replicas, set_range=True)
    except IntegrityError:
        await db.rollback()
        # A concurrent delete can race the INSERT/UPDATE of apply_scale's commit
        # (Postgres/SQLite); same parity as api/instances.py deploy.
        raise HTTPException(status_code=409, detail="allocation conflict; retry") from None
    # Re-read after apply_scale's commit (expired attributes cannot be lazy-loaded in async).
    try:
        rows = await db.execute(
            select(Deployment).options(*_DEPLOYMENT_LOADS).where(Deployment.id == deployment_id)
        )
        refreshed = rows.scalar_one()
    except NoResultFound:
        # The group was deleted between apply_scale's commit and this re-read.
        raise HTTPException(status_code=404, detail="deployment not found") from None
    _with_names(list(refreshed.instances))
    return refreshed


@router.delete("/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deployment(
    deployment_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    deployment = await db.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    # Instances are removed by the ORM cascade ("all, delete-orphan"); the worker cleans up
    # its containers on its own (delete command for DB-unknown instances already exists).
    await db.delete(deployment)
    await db.commit()
