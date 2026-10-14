"""Model catalog endpoints: read for any authenticated user, write for admins."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.auth import get_current_user, require_admin
from inferna_server.db import get_db
from inferna_server.models import Deployment, Model, User
from inferna_server.schemas import ModelCreate, ModelOut

router = APIRouter(prefix="/models", tags=["models"])

# Engines that can actually serve each category; kept in step with fixtures/catalog.json and
# docs/engine-matrix.md (audio and reranker-vllm have no supported engine yet).
CATEGORY_ENGINES: dict[str, list[str]] = {
    "llm": ["vllm", "sglang"],
    "embedding": ["vllm", "sglang"],
    "multimodal": ["vllm", "sglang"],
    "reranker": ["sglang"],
    "audio": [],
}


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


@router.post("", response_model=ModelOut, status_code=201)
async def create_model(
    body: ModelCreate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Model:
    """Add a Hugging Face repository to the catalog.

    `seed_catalog` upserts builtin entries by name on every start, so a custom model must not
    reuse a builtin name — it would be silently overwritten on the next boot.
    """
    name = body.hf_repo_id.strip()
    existing = (await db.execute(select(Model).where(Model.name == name))).scalar_one_or_none()
    if existing is not None:
        if existing.is_builtin:
            raise HTTPException(status_code=409, detail="name is reserved by a builtin model")
        raise HTTPException(status_code=409, detail="model already exists")

    model = Model(
        name=name,
        display_name=body.display_name or name,
        category=body.category,
        description=body.description,
        params_b=None,
        vram_required_mb=body.vram_required_mb,
        requires_hf_token=body.requires_hf_token,
        license=body.license,
        is_builtin=False,
        supported_engines=CATEGORY_ENGINES[body.category],
    )
    db.add(model)
    try:
        await db.commit()
    except IntegrityError:
        # Two admins creating the same repository concurrently: the unique index wins the race.
        await db.rollback()
        raise HTTPException(status_code=409, detail="model already exists") from None
    await db.refresh(model)
    return model


@router.delete("/{model_id}", status_code=204)
async def delete_model(
    model_id: uuid.UUID,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    model = await db.get(Model, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    if model.is_builtin:
        raise HTTPException(status_code=409, detail="builtin models cannot be deleted")
    deployment = (
        await db.execute(select(Deployment.id).where(Deployment.model_id == model_id).limit(1))
    ).scalar_one_or_none()
    if deployment is not None:
        raise HTTPException(status_code=409, detail="model has deployments")
    await db.delete(model)
    await db.commit()
