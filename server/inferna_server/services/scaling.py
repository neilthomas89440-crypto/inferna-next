"""Manual scaling of deployment replica groups (Release A)."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.models import LIVE_STATES, Deployment, ModelInstance, utcnow
from inferna_server.services.scheduler import (
    allocate_parallel_replicas,
    allocate_replicas,
    rendezvous_host,
)

logger = structlog.get_logger(__name__)


def replica_unit_count(deployment: Deployment) -> int:
    """How many replica units the deployment has: instances, or whole groups for multi-node.

    Anything that clamps a replica count into a window has to use *this* number: the endpoint that
    sets an autoscaling window used instance rows, so a two-rank group looked like two replicas and
    a widen-the-window request tried to allocate a second group out of nowhere.
    """
    return len(_replica_units(deployment))


def _replica_units(deployment: Deployment) -> list[list[ModelInstance]]:
    """Group instances into replica units: one instance, or one whole parallel group."""
    if deployment.nodes_per_replica <= 1:
        return [[inst] for inst in deployment.instances]
    groups: dict[object, list[ModelInstance]] = {}
    for inst in deployment.instances:
        groups.setdefault(inst.parallel_group_id or inst.id, []).append(inst)
    return list(groups.values())


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
    # Classify desired-running replicas: only state=="running" serves traffic now;
    # pending ones (scheduled/starting — everything non-terminal in LIVE_STATES)
    # have not come up yet but are on track and count toward the target capacity.
    # Terminally errored replicas hold a port/GPU slot while serving nothing, so
    # they must not count as capacity.
    # A replica *unit* is one instance for single-node deployments and one whole parallel group
    # for multi-node ones: ranks must start and stop together, and counting them separately is
    # exactly how half groups get created.
    units = _replica_units(deployment)
    live = [unit for unit in units if any(inst.desired_state == "running" for inst in unit)]
    serving = [unit for unit in live if all(inst.state == "running" for inst in unit)]
    pending = [
        unit
        for unit in live
        if unit not in serving and all(inst.state in LIVE_STATES for inst in unit)
    ]
    broken = [unit for unit in live if any(inst.state == "error" for inst in unit)]
    # Stop (not delete) the broken replicas exactly like a scale-in would: the
    # generation bump tells the worker to tear down the container and free its
    # port/GPU slot; the record stays for auditability. Replacing them via fresh
    # allocate_replicas records yields a clean placement — auto-restarting the
    # same record is Release B scope.
    for unit in broken:
        for inst in unit:
            inst.desired_state = "stopped"
            inst.generation += 1
            inst.error_detail = None
    effective_capacity = len(serving) + len(pending)
    action = "out" if replicas > effective_capacity else "in"
    if replicas > effective_capacity:
        # Anti-affinity against the GPU pairs already held by this group.
        used = {
            (inst.worker_id, index)
            for unit in (*serving, *pending)
            for inst in unit
            for index in inst.gpu_indexes
            if inst.worker_id
        }
        new_units = replicas - effective_capacity
        if deployment.nodes_per_replica > 1:
            occupied = {inst.worker_id for unit in units for inst in unit if inst.worker_id}
            for _ in range(new_units):
                allocations = await allocate_parallel_replicas(
                    db,
                    deployment.cluster_id,
                    deployment.model.vram_required_mb,
                    deployment.engine,
                    deployment.nodes_per_replica,
                    exclude_worker_ids=occupied,
                )
                head = allocations[0]
                rendezvous = (
                    f"{rendezvous_host(head.worker.address, head.worker.hostname)}"
                    f":{head.rendezvous_port}"
                )
                group_id = uuid.uuid4()
                for allocation in allocations:
                    deployment.instances.append(
                        ModelInstance(
                            model_id=deployment.model_id,
                            cluster_id=deployment.cluster_id,
                            worker_id=allocation.worker.id,
                            engine=deployment.engine,
                            profile=deployment.profile,
                            gpu_indexes=allocation.gpu_indexes,
                            state="scheduled",
                            desired_state="running",
                            generation=1,
                            port=allocation.instance_port,
                            parallel_group_id=group_id,
                            node_rank=allocation.node_rank,
                            world_size=deployment.nodes_per_replica,
                            parallel_mode=deployment.parallel_mode,
                            rendezvous=rendezvous,
                        )
                    )
                    occupied.add(allocation.worker.id)
        else:
            allocations = await allocate_replicas(
                db,
                deployment.cluster_id,
                deployment.model.vram_required_mb,
                deployment.engine,
                new_units,
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
    elif replicas < effective_capacity:
        # Stop non-running replicas first (starting/scheduled), then oldest running —
        # exactly like POST /{id}/stop so the worker receives the stop command via
        # generation. Broken replicas are already stopped above and hold no capacity.
        candidates = serving + pending
        # Partial groups are stopped first, then the oldest complete ones — the same rule as
        # before, now applied to units so a group is never left half-standing.
        to_stop = sorted(
            candidates,
            key=lambda unit: (
                all(inst.state == "running" for inst in unit),
                min(inst.created_at for inst in unit),
            ),
        )[: effective_capacity - replicas]
        for unit in to_stop:
            for inst in unit:
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
        from_capacity=effective_capacity,
        to_count=replicas,
        action=action,
        broken_stopped=len(broken),
        set_range=set_range,
    )
    await db.commit()
