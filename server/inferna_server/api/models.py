"""Model catalog endpoints (any authenticated user)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import Model, User
from inferna_server.schemas import ModelOut

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelOut])
async def list_models(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Model]:
    rows = await db.execute(select(Model).order_by(Model.display_name))
    return list(rows.scalars().all())


@router.get("/{model_id}", response_model=ModelOut)
async def get_model(
    model_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Model:
    model = await db.get(Model, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    return model
