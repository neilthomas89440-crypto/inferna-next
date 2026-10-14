"""Usage endpoint: aggregated gateway consumption per day, model, key and user (read-only)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.auth import get_current_user
from inferna_server.db import get_db
from inferna_server.models import ApiKey, GpuUsageRecord, UsageRecord, User
from inferna_server.schemas import UsageLimits, UsageOut, UsageRow, UsageTotals
from inferna_server.services.usage import usage_today

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageOut)
async def usage(
    days: int = Query(7, ge=1, le=90),
    model_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageOut:
    """Daily aggregates. Admins see every user; everyone else sees only their own rows."""
    period_from = datetime.now(timezone.utc).date() - timedelta(days=days - 1)

    conditions = [UsageRecord.period_start >= period_from]
    if model_id is not None:
        conditions.append(UsageRecord.model_id == model_id)
    if current.role != "admin":
        conditions.append(UsageRecord.user_id == current.id)
    if key_id is not None:
        if current.role != "admin":
            key = await db.get(ApiKey, key_id)
            if key is None or key.user_id != current.id:
                raise HTTPException(status_code=403, detail="not allowed")
        conditions.append(UsageRecord.api_key_id == key_id)

    records = (
        (
            await db.execute(
                select(UsageRecord)
                .where(*conditions)
                .options(
                    selectinload(UsageRecord.model),
                    selectinload(UsageRecord.user),
                    selectinload(UsageRecord.api_key),
                )
                .order_by(UsageRecord.period_start.desc(), UsageRecord.requests.desc())
            )
        )
        .scalars()
        .all()
    )

    gpu_seconds = await _gpu_seconds(db, {record.model_id for record in records}, period_from)

    rows = [
        UsageRow(
            period_start=record.period_start,
            model_id=record.model_id,
            model_name=record.model.name if record.model else "unknown",
            user_id=record.user_id,
            username=record.user.username if record.user else "unknown",
            api_key_id=record.api_key_id,
            requests=record.requests,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            avg_latency_ms=(
                round(record.latency_ms_sum / record.requests) if record.requests else 0
            ),
            gpu_hours=round(
                gpu_seconds.get((record.model_id, record.period_start), 0.0) / 3600.0, 3
            ),
        )
        for record in records
    ]

    requests_used, tokens_used = await usage_today(db, current.id)
    totals = UsageTotals(
        requests=sum(row.requests for row in rows),
        tokens_in=sum(row.tokens_in for row in rows),
        tokens_out=sum(row.tokens_out for row in rows),
        avg_latency_ms=(
            round(
                sum(record.latency_ms_sum for record in records)
                / max(sum(record.requests for record in records), 1)
            )
        ),
        gpu_hours=round(sum(gpu_seconds.values()) / 3600.0, 3),
    )
    limits = UsageLimits(
        token_limit=current.token_limit,
        request_limit=current.request_limit,
        requests_used=requests_used,
        tokens_used=tokens_used,
    )
    return UsageOut(totals=totals, rows=rows, limits=limits)


async def _gpu_seconds(
    db: AsyncSession, model_ids: set[uuid.UUID], period_from: date
) -> dict[tuple[uuid.UUID, date], float]:
    """GPU-seconds per (model, day) for the requested window."""
    if not model_ids:
        return {}
    query = select(GpuUsageRecord).where(
        GpuUsageRecord.period_start >= period_from,
        GpuUsageRecord.model_id.in_(model_ids),
    )
    rows = (await db.execute(query)).scalars().all()
    totals: dict[tuple[uuid.UUID, date], float] = {}
    for row in rows:
        key = (row.model_id, row.period_start)
        totals[key] = totals.get(key, 0.0) + row.gpu_seconds
    return totals
