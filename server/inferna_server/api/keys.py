"""API key management (self or admin)."""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import ApiKey, User, utcnow
from inferna_server.schemas import ApiKeyCreate, ApiKeyOut, ApiKeySecretOut
from inferna_server.services.workers_svc import sha256_hex

router = APIRouter(prefix="/keys", tags=["keys"])


def generate_api_key() -> str:
    return "inf-" + secrets.token_hex(16)


@router.post("", response_model=ApiKeySecretOut, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeySecretOut:
    key = generate_api_key()
    api_key = ApiKey(
        user_id=current.id,
        name=body.name,
        key_hash=sha256_hex(key),
        scopes=["inference"],
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    base = ApiKeyOut.model_validate(api_key, from_attributes=True)
    return ApiKeySecretOut(**base.model_dump(), key=key)


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKey]:
    if current.role == "admin":
        rows = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    else:
        rows = await db.execute(
            select(ApiKey).where(ApiKey.user_id == current.id).order_by(ApiKey.created_at.desc())
        )
    return list(rows.scalars().all())


@router.post(
    "/{key_id}/revoke",
    response_model=ApiKeyOut,
    responses={404: {"description": "Key not found"}},
)
async def revoke_key(
    key_id: uuid.UUID,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if current.role != "admin" and key.user_id != current.id:
        raise HTTPException(status_code=403, detail="not allowed")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await db.commit()
        await db.refresh(key)
    return key
