"""Usage aggregation for the gateway and the daily GPU-occupancy sampler.

The gateway is a hot path, so it only appends to a bounded in-memory buffer
(`record_request`, synchronous, no awaits). A background loop folds that buffer into
`usage_records` rows (UPSERT per UTC day and user/key/model) and samples GPU occupancy
into `gpu_usage_records`. A crash loses at most one interval of traffic — the same
trade-off the Prometheus counters already make on restart.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.db import SessionLocal
from inferna_server.models import (
    LIVE_STATES,
    GpuUsageRecord,
    ModelInstance,
    UsageRecord,
    User,
)
from inferna_server.services.metrics import inferna_usage_events_total

logger = structlog.get_logger(__name__)

USAGE_FLUSH_INTERVAL_SECONDS = 30.0
# Guard rail: a runaway number of distinct (user, key, model, day) tuples would grow memory
# without bound. Reaching this in one interval means something is wrong, so we drop and count.
MAX_PENDING_KEYS = 10_000


@dataclass(slots=True)
class UsageContext:
    """What the gateway knows about a request, carried to its accounting point."""

    user_id: uuid.UUID
    api_key_id: uuid.UUID
    model_id: uuid.UUID
    model_name: str
    instance_id: uuid.UUID
    started: float
    # Serving telemetry labels: the engine that handled the request and the topology it runs in
    # ("single" until the multi-node contract adds `parallel_mode`).
    engine: str = "unknown"
    parallelism: str = "single"


@dataclass(slots=True)
class _Accum:
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms_sum: int = 0

    def add(self, other: _Accum) -> None:
        self.requests += other.requests
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.latency_ms_sum += other.latency_ms_sum


_PENDING: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID, date], _Accum] = {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def record_request(
    ctx: UsageContext,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    outcome: str = "ok",
) -> None:
    """Account one finished (or aborted) gateway request.

    Called from the streaming code path, so it stays synchronous: the body has no `await`,
    which makes the dictionary update atomic on the event loop thread.
    """
    inferna_usage_events_total.labels(ctx.model_name, outcome).inc()
    key = (ctx.user_id, ctx.api_key_id, ctx.model_id, _today())
    accum = _PENDING.get(key)
    if accum is None:
        if len(_PENDING) >= MAX_PENDING_KEYS:
            inferna_usage_events_total.labels(ctx.model_name, "dropped").inc()
            logger.warning("usage buffer full, dropping aggregate", pending=len(_PENDING))
            return
        accum = _Accum()
        _PENDING[key] = accum
    accum.requests += 1
    accum.tokens_in += max(tokens_in, 0)
    accum.tokens_out += max(tokens_out, 0)
    accum.latency_ms_sum += max(latency_ms, 0)


def pending_snapshot() -> dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID, date], _Accum]:
    """Copy of the buffer (tests and the reconciler read it; never mutate the result)."""
    return dict(_PENDING)


async def flush_usage(db: AsyncSession) -> int:
    """Fold buffered aggregates into `usage_records`. Returns rows written.

    The buffer is swapped out before the first await, so requests arriving during the flush
    land in the fresh buffer. On failure the swapped aggregates are merged back.
    """
    global _PENDING
    pending, _PENDING = _PENDING, {}
    if not pending:
        return 0

    written = 0
    try:
        for (user_id, api_key_id, model_id, period_start), accum in pending.items():
            row = (
                await db.execute(
                    select(UsageRecord).where(
                        UsageRecord.user_id == user_id,
                        UsageRecord.api_key_id == api_key_id,
                        UsageRecord.model_id == model_id,
                        UsageRecord.period_start == period_start,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                db.add(
                    UsageRecord(
                        user_id=user_id,
                        api_key_id=api_key_id,
                        model_id=model_id,
                        period_start=period_start,
                        requests=accum.requests,
                        tokens_in=accum.tokens_in,
                        tokens_out=accum.tokens_out,
                        latency_ms_sum=accum.latency_ms_sum,
                    )
                )
            else:
                row.requests += accum.requests
                row.tokens_in += accum.tokens_in
                row.tokens_out += accum.tokens_out
                row.latency_ms_sum += accum.latency_ms_sum
            written += 1
        await db.commit()
    except Exception:
        await db.rollback()
        for key, accum in pending.items():
            existing = _PENDING.get(key)
            if existing is None:
                _PENDING[key] = accum
            else:
                existing.add(accum)
        raise
    return written


async def usage_today(db: AsyncSession, user_id: uuid.UUID) -> tuple[int, int]:
    """(requests, tokens) for the current UTC day, including not-yet-flushed aggregates."""
    period = _today()
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(UsageRecord.requests), 0),
                func.coalesce(func.sum(UsageRecord.tokens_in + UsageRecord.tokens_out), 0),
            ).where(UsageRecord.user_id == user_id, UsageRecord.period_start == period)
        )
    ).one()
    requests, tokens = int(row[0]), int(row[1])
    for (pending_user, _key, _model, day), accum in _PENDING.items():
        if pending_user == user_id and day == period:
            requests += accum.requests
            tokens += accum.tokens_in + accum.tokens_out
    return requests, tokens


async def check_quota(db: AsyncSession, user: User) -> tuple[int, int] | None:
    """Return (used, limit) once a soft daily limit is exhausted, otherwise None."""
    if user.token_limit is None and user.request_limit is None:
        return None
    requests, tokens = await usage_today(db, user.id)
    if user.request_limit is not None and requests >= user.request_limit:
        return requests, user.request_limit
    if user.token_limit is not None and tokens >= user.token_limit:
        return tokens, user.token_limit
    return None


async def sample_occupancy(db: AsyncSession, interval_seconds: float) -> int:
    """Attribute `interval_seconds × gpu_count` of every live instance to its model+cluster."""
    instances = (
        (await db.execute(select(ModelInstance).where(ModelInstance.state.in_(LIVE_STATES))))
        .scalars()
        .all()
    )
    per_key: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
    for instance in instances:
        gpu_count = len(instance.gpu_indexes or [])
        if gpu_count == 0:
            continue
        key = (instance.model_id, instance.cluster_id)
        per_key[key] = per_key.get(key, 0.0) + interval_seconds * gpu_count
    if not per_key:
        return 0

    period = _today()
    for (model_id, cluster_id), seconds in per_key.items():
        row = (
            await db.execute(
                select(GpuUsageRecord).where(
                    GpuUsageRecord.model_id == model_id,
                    GpuUsageRecord.cluster_id == cluster_id,
                    GpuUsageRecord.period_start == period,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(
                GpuUsageRecord(
                    model_id=model_id,
                    cluster_id=cluster_id,
                    period_start=period,
                    gpu_seconds=seconds,
                )
            )
        else:
            row.gpu_seconds += seconds
    await db.commit()
    return len(per_key)


async def usage_flush_loop() -> None:
    """Background loop: fold usage and sample occupancy; never dies on a bad tick."""
    while True:
        try:
            async with SessionLocal() as db:
                written = await flush_usage(db)
                if written:
                    logger.info("usage aggregates flushed", rows=written)
                await sample_occupancy(db, USAGE_FLUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("usage flush failed")
        await asyncio.sleep(USAGE_FLUSH_INTERVAL_SECONDS)


def monotonic_ms(started: float) -> int:
    """Latency in whole milliseconds since `started` (monotonic clock)."""
    return int((time.monotonic() - started) * 1000)
