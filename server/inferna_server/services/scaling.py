"""Manual scaling of deployment replica groups (Release A)."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.models import Deployment, ModelInstance, utcnow
from inferna_server.services.scheduler import allocate_replicas

logger = structlog.get_logger(__name__)


async def apply_scale(
    db: AsyncSession, deployment: Deployment, replicas: int, *, set_range: bool = True
) -> None:
    """Scale `deployment` to `replicas` live replicas and commit exactly once.

    With set_range=True (manual semantics) the group is pinned: min=max=replicas.
    With set_range=False (autoscaler semantics, Release B) only the replica count
    moves — within the [min_replicas, max_replicas] range the user configured.
    """
    # Serialize the read-modify-write on the group: concurrent scale requests all
    # load the deployment with a plain SELECT before any of them commits, so each
    # would compute `live` from the same stale snapshot (lost update). Locking the
    # workers in allocate_replicas serializes only allocation, not this decision,
    # and scale-in takes no locks at all. Lock the deployment row first, then
    # re-read the replica collection inside the lock; "deployment row → workers"
    # is a lock order no other path uses (the scheduler never touches deployments),
    # so there is no deadlock cycle. On SQLite FOR UPDATE is a no-op.
    await db.execute(
        select(Deployment).where(Deployment.id == deployment.id).with_for_update()
    )
    await db.refresh(deployment, attribute_names=["instances"])
    if not set_range:
        # An autoscaler proposal (Release B) may fall outside the configured
        # window; clamp it so the commit cannot violate ck_deployments_min_max
        # (IntegrityError → 500). min/max themselves stay untouched — with
        # set_range=False only the live replica count moves.
        replicas = max(deployment.min_replicas, min(deployment.max_replicas, replicas))
    live = [inst for inst in deployment.instances if inst.desired_state == "running"]
    action = "out" if replicas > len(live) else "in"
    if replicas > len(live):
        # Anti-affinity against the GPU pairs already held by this group.
        used = {
            (inst.worker_id, index) for inst in live for index in inst.gpu_indexes if inst.worker_id
        }
        allocations = await allocate_replicas(
            db,
            deployment.cluster_id,
            deployment.model.vram_required_mb,
            deployment.engine,
            replicas - len(live),
            used,
        )
        for worker, gpu_indexes, port in allocations:
            deployment.instances.append(
                ModelInstance(
                    model_id=deployment.model_id,
                    cluster_id=deployment.cluster_id,
                    worker_id=worker.id,
                    engine=deployment.engine,
                    profile=deployment.profile,
                    gpu_indexes=gpu_indexes,
                    state="scheduled",
                    desired_state="running",
                    generation=1,
                    port=port,
                )
            )
    elif replicas < len(live):
        # Stop non-running replicas first (error/starting/scheduled), then oldest running —
        # exactly like POST /{id}/stop so the worker receives the stop command via generation.
        to_stop = sorted(live, key=lambda inst: (inst.state == "running", inst.created_at))[
            : len(live) - replicas
        ]
        for inst in to_stop:
            inst.desired_state = "stopped"
            inst.generation += 1
            inst.error_detail = None
    if set_range:
        deployment.min_replicas = replicas
        deployment.max_replicas = replicas
    deployment.last_scaled_at = utcnow()
    logger.info(
        "deployment scaled",
        deployment_id=str(deployment.id),
        model=deployment.model.name,
        from_count=len(live),
        to_count=replicas,
        action=action,
        set_range=set_range,
    )
    await db.commit()
