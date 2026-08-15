"""User management (admin) + password change (self or admin)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.auth import get_current_user, hash_password, require_admin
from inferna_server.db import get_db
from inferna_server.models import User
from inferna_server.schemas import PasswordChange, UserCreate, UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    rows = await db.execute(select(User).order_by(User.username))
    return list(rows.scalars().all())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> User:
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")
    existing = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="username already taken")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    if current.id == user_id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    admin_count = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True))
        )
    ).scalar_one()
    if user.role == "admin" and admin_count <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    await db.delete(user)
    await db.commit()


@router.put("/{user_id}/password", response_model=UserOut)
async def change_password(
    user_id: uuid.UUID,
    body: PasswordChange,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    if current.role != "admin" and current.id != user_id:
        raise HTTPException(status_code=403, detail="not allowed")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.password_hash = hash_password(body.password)
    await db.commit()
    await db.refresh(user)
    return user
