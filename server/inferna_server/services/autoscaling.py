"""Autoscaling policy and loop (Release B).

One rule keeps this honest: **the policy reads measured load, never a proxy for it**. It uses the
gateway's own TTFT histogram (M1.2) and the per-instance active-request gauge, both already
published, and it moves at most one replica per tick with a cooldown — an autoscaler that can jump
is an autoscaler that oscillates.

Replica units are the unit of scaling: one instance for a single-node deployment, one whole parallel
group for a multi-node one (`services/scaling.py`), so a tick can never leave half a group.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.db import SessionLocal
from inferna_server.models import LIVE_STATES, Deployment, ModelInstance
from inferna_server.services.metrics import (
    inferna_autoscale_actions_total,
    inferna_autoscale_signal_ratio,
    inferna_autoscale_target_replicas,
)
from inferna_server.services.scaling import apply_scale

if TYPE_CHECKING:  # pragma: no cover - typing only
    from inferna_server.config import Settings

logger = structlog.get_logger(__name__)

# Histogram buckets of `inferna_ttft_seconds` (services/metrics.py) — keep in step with it.
TTFT_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0)


@dataclass(slots=True)
class Signal:
    """What the policy is allowed to look at for one deployment."""

    ttft_p95_ms: float | None
    queue_per_replica: float | None
    serving_replicas: int


def hist_p95(buckets: tuple[float, ...], counts: list[float]) -> float | None:
    """p95 from cumulative bucket counts (Prometheus-style), or None without observations.

    The same interpolation the bench harness uses locally, so a number computed here and a number
    computed from `/metrics` agree rather than merely correlate.
    """
    if not counts or counts[-1] <= 0:
        return None
    total = counts[-1]
    target = 0.95 * total
    previous_bound, previous_count = 0.0, 0.0
    for bound, count in zip(buckets, counts, strict=False):
        if count >= target:
            if count == previous_count:
                return bound * 1000
            span = count - previous_count
            value = previous_bound + (bound - previous_bound) * ((target - previous_count) / span)
            return value * 1000
        previous_bound, previous_count = bound, count
    return buckets[-1] * 1000


def _ttft_p95_ms(model_name: str) -> float | None:
    """p95 TTFT for a model from the gateway histogram, summed over engines and topologies."""
    counts: list[float] = []
    for upper in TTFT_BUCKETS:
        value = REGISTRY.get_sample_value(
            "inferna_ttft_seconds_bucket",
            {"model": model_name, "engine": "vllm", "parallelism": "single", "le": str(upper)},
        )
        if value is None:
            # Labels vary (engine/topology); fall back to scanning the raw metric families.
            return _scan_ttft(model_name)
        counts.append(value)
    return hist_p95(TTFT_BUCKETS, counts)


def _scan_ttft(model_name: str) -> float | None:
    """Fallback: sum every `inferna_ttft_seconds_bucket` line for this model across labels."""
    totals: dict[float, float] = dict.fromkeys(TTFT_BUCKETS, 0.0)
    seen = False
    for metric in REGISTRY.collect():
        if metric.name != "inferna_ttft_seconds":
            continue
        for sample in metric.samples:
            if sample.name != "inferna_ttft_seconds_bucket":
                continue
            labels = sample.labels
            if labels.get("model") != model_name:
                continue
            try:
                upper = float(labels.get("le", ""))
            except ValueError:
                continue
            if upper in totals:
                totals[upper] += sample.value
                seen = True
    if not seen:
        return None
    return hist_p95(TTFT_BUCKETS, [totals[upper] for upper in TTFT_BUCKETS])


def read_signal(deployment: Deployment, instances: list[ModelInstance]) -> Signal:
    """Measured load for a deployment: TTFT p95 for its model, queue depth per serving replica."""
    serving = [inst for inst in instances if inst.state == "running"]
    queue_total = 0.0
    queue_seen = False
    for instance in serving:
        value = REGISTRY.get_sample_value(
            "inferna_instance_active_requests",
            {"instance_id": str(instance.id), "model": deployment.model.name},
        )
        if value is None:
            # Label sets differ between versions of the metric; fall back to any match by id.
            value = _scan_active(instance.id)
        if value is not None:
            queue_total += value
            queue_seen = True
    return Signal(
        ttft_p95_ms=_ttft_p95_ms(deployment.model.name),
        queue_per_replica=(queue_total / len(serving)) if serving and queue_seen else None,
        serving_replicas=len(serving),
    )


def _scan_active(instance_id: uuid.UUID) -> float | None:
    for metric in REGISTRY.collect():
        if metric.name != "inferna_instance_active_requests":
            continue
        for sample in metric.samples:
            if sample.labels.get("instance_id") == str(instance_id):
                return float(sample.value)
    return None


def desired_replicas(
    *,
    current: int,
    window: tuple[int, int],
    signal: Signal,
    settings: Settings,
) -> int:
    """Next replica count: one step per tick, inside the window, never on missing data."""
    minimum, maximum = window
    target_ttft = settings.autoscale_target_ttft_ms
    target_queue = settings.autoscale_target_queue
    ratio = settings.autoscale_scale_down_ratio

    if signal.ttft_p95_ms is not None:
        inferna_autoscale_signal_ratio.labels("deployment", "ttft").set(
            signal.ttft_p95_ms / target_ttft if target_ttft else 0.0
        )
    if signal.queue_per_replica is not None:
        inferna_autoscale_signal_ratio.labels("deployment", "queue").set(
            signal.queue_per_replica / target_queue if target_queue else 0.0
        )

    # No data is not a reason to act: an autoscaler that guesses on absent metrics is worse than
    # one that waits for the next tick.
    if signal.ttft_p95_ms is None and signal.queue_per_replica is None:
        return current

    busy = (signal.ttft_p95_ms is not None and signal.ttft_p95_ms > target_ttft) or (
        signal.queue_per_replica is not None and signal.queue_per_replica > target_queue
    )
    idle = (
        (signal.ttft_p95_ms is None or signal.ttft_p95_ms < target_ttft * ratio)
        and (signal.queue_per_replica is None or signal.queue_per_replica < target_queue * ratio)
        and signal.serving_replicas >= minimum
    )
    if busy and current < maximum:
        return min(maximum, current + 1)
    if idle and current > minimum:
        return max(minimum, current - 1)
    return current


async def autoscale_tick(
    db: AsyncSession, cooldown: dict[uuid.UUID, float], *, now: float | None = None
) -> list[tuple[uuid.UUID, str, int]]:
    """One pass over deployments with a window; returns (deployment, action, target) rows."""
    now = time.monotonic() if now is None else now
    rows = (
        (
            await db.execute(
                select(Deployment)
                .where(Deployment.max_replicas > Deployment.min_replicas)
                .options(
                    selectinload(Deployment.model),
                    selectinload(Deployment.instances),
                )
            )
        )
        .scalars()
        .all()
    )
    actions: list[tuple[uuid.UUID, str, int]] = []
    for deployment in rows:
        settings = _settings()
        window = (deployment.min_replicas, deployment.max_replicas)
        units = _unit_count(deployment)
        signal = read_signal(deployment, list(deployment.instances))
        target = desired_replicas(current=units, window=window, signal=signal, settings=settings)
        # No previous action means no cooldown. Comparing against a default of 0.0 would suppress
        # every action on a machine whose monotonic clock has not passed the cooldown yet — which
        # is exactly what a fresh CI container looks like.
        last = cooldown.get(deployment.id)
        if target == units or (
            last is not None and now - last < settings.autoscale_cooldown_seconds
        ):
            inferna_autoscale_actions_total.labels(str(deployment.id), "none").inc()
            continue
        action = "up" if target > units else "down"
        try:
            await apply_scale(db, deployment, target, set_range=False)
        except Exception:  # noqa: BLE001 - a failed scale must not kill the loop
            logger.exception("autoscale failed", deployment_id=str(deployment.id), target=target)
            inferna_autoscale_actions_total.labels(str(deployment.id), "failed").inc()
            continue
        cooldown[deployment.id] = now
        inferna_autoscale_actions_total.labels(str(deployment.id), action).inc()
        inferna_autoscale_target_replicas.labels(str(deployment.id)).set(target)
        logger.info(
            "autoscaled",
            deployment_id=str(deployment.id),
            action=action,
            replicas=units,
            target=target,
            ttft_p95_ms=signal.ttft_p95_ms,
            queue_per_replica=signal.queue_per_replica,
        )
        actions.append((deployment.id, action, target))
    return actions


def _unit_count(deployment: Deployment) -> int:
    """Replica units currently desired: groups for multi-node, instances otherwise."""
    live = [inst for inst in deployment.instances if inst.desired_state == "running"]
    if deployment.nodes_per_replica <= 1:
        return len(live)
    return len({inst.parallel_group_id or inst.id for inst in live})


def _settings() -> Settings:
    from inferna_server.config import get_settings  # local import: keeps the module import-light

    return get_settings()


async def autoscale_loop() -> None:
    """Background loop: one tick per interval; never dies on a bad tick."""
    cooldown: dict[uuid.UUID, float] = {}
    while True:
        settings = _settings()
        await asyncio.sleep(settings.autoscale_interval_seconds)
        if not settings.autoscale_enabled:
            continue
        try:
            async with SessionLocal() as db:
                actions = await autoscale_tick(db, cooldown)
                if actions:
                    logger.info("autoscale pass done", actions=len(actions))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("autoscale tick failed")


def live_instances(instances: list[ModelInstance]) -> list[ModelInstance]:
    """Non-terminal instances (used by the signal reader and by tests)."""
    return [inst for inst in instances if inst.state in LIVE_STATES]
