"""Worker lifecycle: gRPC register/sync logic, catalog seeding, disconnect detection."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grpc
import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inferna_server.config import get_settings
from inferna_server.models import (
    LIVE_STATES,
    Cluster,
    Model,
    ModelInstance,
    Worker,
    WorkerGPU,
)
from inferna_server.proto import cluster_pb2
from inferna_server.services.upstream_guard import validate_worker_address

logger = structlog.get_logger(__name__)

SYNC_INTERVAL_SECONDS = 5
DISCONNECT_AFTER_SECONDS = 30
DISCONNECT_CHECK_INTERVAL_SECONDS = 15
# Worker sends the last ~20 container log lines; cap defensively so a chatty engine
# cannot grow a row without bound (keeps the newest bytes).
LOG_TAIL_MAX_CHARS = 8192

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_worker_token() -> str:
    return secrets.token_hex(16)  # 32 hex chars


def grpc_error(code: grpc.StatusCode, detail: str) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata(), detail)


def _apply_parallelism(config: cluster_pb2.EngineConfig, instance: ModelInstance) -> None:
    """Fill `Parallelism`/`Rendezvous` for a rank of a parallel group.

    Single-node instances carry no group id and leave the two messages empty, which is exactly
    the pre-multi-node contract — the worker's command line does not change for them.

    `MASTER_ADDR`/`MASTER_PORT` travel in `Rendezvous.env` so the address a rank must dial is
    declared by the server (invariant §3.4). The interface/`VLLM_HOST_IP` stay worker-side
    decisions: only the worker knows its NICs.
    """
    if instance.parallel_group_id is None or instance.rendezvous is None:
        return
    host, _, port = instance.rendezvous.rpartition(":")
    node_rank = instance.node_rank or 0
    world_size = instance.world_size or 1
    config.parallelism.CopyFrom(
        cluster_pb2.Parallelism(
            mode=instance.parallel_mode or "single",
            tensor_parallel=len(instance.gpu_indexes or []),
            pipeline_parallel=world_size if instance.parallel_mode == "pp" else 1,
            world_size=world_size,
        )
    )
    config.rendezvous.CopyFrom(
        cluster_pb2.Rendezvous(
            master_address=host,
            master_port=int(port) if port.isdigit() else 0,
            node_rank=node_rank,
            num_nodes=world_size,
            env=[f"MASTER_ADDR={host}", f"MASTER_PORT={port}"],
        )
    )


async def register_worker(
    db: AsyncSession, request: cluster_pb2.RegisterRequest
) -> cluster_pb2.RegisterResponse:
    settings = get_settings()
    if request.cluster_token != settings.registration_token:
        raise grpc_error(grpc.StatusCode.PERMISSION_DENIED, "invalid cluster token")
    # Protocol / version compatibility (B.7)
    from inferna_server.version import MIN_WORKER_VERSION, PROTOCOL_VERSION

    if request.protocol_version != PROTOCOL_VERSION:
        raise grpc_error(
            grpc.StatusCode.FAILED_PRECONDITION,
            f"unsupported protocol version {request.protocol_version}; "
            f"server supports {PROTOCOL_VERSION}",
        )
    version_str = request.version or "0.0.0"
    try:
        ver_tuple = tuple(int(p) for p in version_str.split(".")[:3])
        # pad to 3 elements
        if len(ver_tuple) < 3:
            ver_tuple = ver_tuple + (0,) * (3 - len(ver_tuple))
    except ValueError:
        ver_tuple = (0, 0, 0)
    if ver_tuple < MIN_WORKER_VERSION:
        raise grpc_error(
            grpc.StatusCode.FAILED_PRECONDITION,
            f"worker version {request.version or 'unknown'} is too old; "
            f"minimum {'.'.join(map(str, MIN_WORKER_VERSION))}",
        )
    cluster_name = request.cluster_name or "default"
    cluster = (
        await db.execute(select(Cluster).where(Cluster.name == cluster_name))
    ).scalar_one_or_none()
    if cluster is None:
        raise grpc_error(grpc.StatusCode.NOT_FOUND, f"cluster '{cluster_name}' not found")

    raw_address = request.address.strip()
    address: str | None = None
    if raw_address:
        try:
            address = await validate_worker_address(raw_address, settings)
        except ValueError as exc:
            raise grpc_error(
                grpc.StatusCode.INVALID_ARGUMENT, f"invalid worker address: {exc}"
            ) from None

    token = generate_worker_token()
    name = request.worker_name or request.hostname
    # Both (cluster, name) and (cluster, hostname) are unique, so identity resolution has to
    # respect both: prefer the machine (hostname), fall back to the configured name, and never
    # write a value that another row already owns. Getting this wrong used to insert a
    # duplicate name (IntegrityError → INTERNAL to the worker, which then retried forever).
    worker = (
        await db.execute(
            select(Worker).where(
                Worker.cluster_id == cluster.id,
                Worker.name == name,
                Worker.hostname == request.hostname,
            )
        )
    ).scalar_one_or_none()
    matched_by = "both" if worker is not None else ""
    if worker is None:
        worker = (
            await db.execute(
                select(Worker).where(
                    Worker.cluster_id == cluster.id, Worker.hostname == request.hostname
                )
            )
        ).scalar_one_or_none()
        matched_by = "hostname" if worker is not None else ""
    if worker is None:
        worker = (
            await db.execute(
                select(Worker).where(Worker.cluster_id == cluster.id, Worker.name == name)
            )
        ).scalar_one_or_none()
        matched_by = "name" if worker is not None else ""

    async def _available(field: str, value: str) -> bool:
        """True when no other row in the cluster already uses `value` for `field`."""
        assert worker is not None
        column = Worker.name if field == "name" else Worker.hostname
        taken = (
            await db.execute(
                select(Worker.id).where(
                    Worker.cluster_id == cluster.id, column == value, Worker.id != worker.id
                )
            )
        ).scalar_one_or_none()
        return taken is None

    if worker is None:
        worker = Worker(
            cluster_id=cluster.id,
            hostname=request.hostname,
            name=name,
            state="connected",
            token_hash=sha256_hex(token),
            version=request.version or None,
            address=address,
            last_seen_at=utcnow(),
        )
        db.add(worker)
    else:
        # Reuse the existing worker (same identity): rotate its token and refresh what the
        # worker reports about itself.
        worker.token_hash = sha256_hex(token)
        worker.state = "connected"
        worker.last_seen_at = utcnow()
        worker.address = address
        if matched_by == "name" and await _available("hostname", request.hostname):
            # The machine moved (new container/renamed host): follow it.
            worker.hostname = request.hostname
        if request.worker_name and request.worker_name != worker.name:
            if await _available("name", request.worker_name):
                worker.name = request.worker_name
            else:
                logger.warning(
                    "worker name already taken; keeping the existing name",
                    worker_id=str(worker.id),
                    requested=request.worker_name,
                )
        if request.version:
            worker.version = request.version
    try:
        await db.commit()
    except IntegrityError:
        # Two workers registering the same name concurrently: the unique index wins, and the
        # loser gets a retryable answer instead of an INTERNAL error.
        await db.rollback()
        raise grpc_error(
            grpc.StatusCode.ALREADY_EXISTS,
            "another worker registered this name concurrently; retry",
        ) from None
    await db.refresh(worker)
    logger.info(
        "worker registered",
        worker_id=str(worker.id),
        hostname=worker.hostname,
        cluster=cluster_name,
    )
    return cluster_pb2.RegisterResponse(
        worker_id=str(worker.id),
        worker_token=token,
        sync_interval_seconds=SYNC_INTERVAL_SECONDS,
    )


async def sync_worker(
    db: AsyncSession, request: cluster_pb2.SyncRequest
) -> cluster_pb2.SyncResponse:
    try:
        worker_id = uuid.UUID(request.worker_id)
    except ValueError:
        raise grpc_error(grpc.StatusCode.UNAUTHENTICATED, "invalid worker id") from None
    worker = (
        await db.execute(
            select(Worker).options(selectinload(Worker.gpus)).where(Worker.id == worker_id)
        )
    ).scalar_one_or_none()
    if worker is None or worker.token_hash != sha256_hex(request.worker_token):
        raise grpc_error(grpc.StatusCode.UNAUTHENTICATED, "invalid worker token")

    # --- system state ---
    system = request.system
    worker.state = "connected"
    worker.last_seen_at = utcnow()
    worker.os = system.os or None
    worker.hostname = system.hostname or worker.hostname
    worker.cpu_cores = system.cpu_cores or None
    worker.memory_mb = system.memory_mb or None

    # --- GPU upsert + stale deletion ---
    existing = {g.index: g for g in worker.gpus}
    reported_indexes = {g.index for g in request.gpus}
    for gpu in request.gpus:
        row = existing.get(gpu.index)
        if row is None:
            row = WorkerGPU(worker_id=worker.id, index=gpu.index)
            db.add(row)
        row.vendor = gpu.vendor
        row.name = gpu.name
        row.vram_mb = gpu.vram_mb
        row.used_vram_mb = gpu.used_vram_mb
        row.utilization_pct = gpu.utilization_pct
        row.uuid = gpu.uuid or None
        row.driver_version = gpu.driver_version or None
    for index in existing:
        if index not in reported_indexes:
            await db.execute(
                delete(WorkerGPU).where(WorkerGPU.worker_id == worker.id, WorkerGPU.index == index)
            )

    # --- reported instance statuses ---
    known: dict[str, ModelInstance] = {}
    if request.instances:
        parsed_ids: list[uuid.UUID] = []
        for status in request.instances:
            try:
                parsed_ids.append(uuid.UUID(status.instance_id))
            except ValueError:
                continue
        if parsed_ids:
            rows = (
                (
                    await db.execute(
                        select(ModelInstance).where(
                            ModelInstance.worker_id == worker.id,
                            ModelInstance.id.in_(parsed_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            known = {str(r.id): r for r in rows}
    for status in request.instances:
        inst = known.get(status.instance_id)
        if inst is None:
            continue
        inst.state = status.state
        inst.error_detail = status.detail or None
        if status.log_tail:
            inst.log_tail = status.log_tail[-LOG_TAIL_MAX_CHARS:]
        if status.port:
            inst.port = status.port

    # --- build commands from desired state ---
    commands: list[cluster_pb2.InstanceCommand] = []
    # Load all instances assigned to this worker
    all_instances = (
        (
            await db.execute(
                select(ModelInstance)
                .options(selectinload(ModelInstance.model))
                .where(ModelInstance.worker_id == worker.id)
            )
        )
        .scalars()
        .all()
    )
    reported: dict[str, cluster_pb2.InstanceStatus] = {s.instance_id: s for s in request.instances}
    for inst in all_instances:
        rep = reported.get(str(inst.id))
        if inst.desired_state == "running":
            applied = rep.generation if rep is not None else 0
            obs = rep.state if rep is not None else "stopped"
            if applied < inst.generation or obs == "stopped":
                config = cluster_pb2.EngineConfig(
                    engine=inst.engine,
                    model_name=inst.model.name,
                    profile=inst.profile,
                    gpu_indexes=inst.gpu_indexes,
                    vram_required_mb=inst.model.vram_required_mb,
                    port=inst.port or 0,
                    requires_hf_token=inst.model.requires_hf_token,
                )
                _apply_parallelism(config, inst)
                commands.append(
                    cluster_pb2.InstanceCommand(
                        instance_id=str(inst.id),
                        action="start",
                        generation=inst.generation,
                        config=config,
                    )
                )
        elif inst.desired_state == "stopped":
            if rep is not None and rep.state in ("starting", "running"):
                commands.append(
                    cluster_pb2.InstanceCommand(instance_id=str(inst.id), action="stop")
                )

    # Instances the worker runs but that no longer exist in the DB → remove.
    known_ids = set(known)
    for status in request.instances:
        if status.instance_id not in known_ids:
            commands.append(
                cluster_pb2.InstanceCommand(instance_id=status.instance_id, action="delete")
            )

    await db.commit()
    return cluster_pb2.SyncResponse(commands=commands)


async def seed_catalog(db: AsyncSession) -> None:
    """Upsert builtin catalog entries from fixtures/catalog.json (mark is_builtin)."""
    catalog = json.loads((FIXTURES_DIR / "catalog.json").read_text(encoding="utf-8"))
    for entry in catalog["models"]:
        # Map catalog field "engines" -> DB column "supported_engines"
        entry_mapped = dict(entry)
        if "engines" in entry_mapped:
            entry_mapped["supported_engines"] = entry_mapped.pop("engines")
        row = (
            await db.execute(select(Model).where(Model.name == entry_mapped["name"]))
        ).scalar_one_or_none()
        if row is None:
            db.add(Model(**entry_mapped, is_builtin=True))
        else:
            for key, value in entry_mapped.items():
                setattr(row, key, value)
            row.is_builtin = True
    await db.flush()


async def mark_disconnected(db: AsyncSession) -> None:
    """Watchdog: stale connected workers → disconnected; their live instances → error."""
    cutoff = utcnow() - timedelta(seconds=DISCONNECT_AFTER_SECONDS)
    stale = (
        (
            await db.execute(
                select(Worker).where(Worker.state == "connected", Worker.last_seen_at < cutoff)
            )
        )
        .scalars()
        .all()
    )
    for worker in stale:
        worker.state = "disconnected"
        live = (
            (
                await db.execute(
                    select(ModelInstance).where(
                        ModelInstance.worker_id == worker.id,
                        ModelInstance.desired_state == "running",
                        ModelInstance.state.in_(LIVE_STATES),
                    )
                )
            )
            .scalars()
            .all()
        )
        for inst in live:
            if inst.state != "stopped":
                inst.state = "error"
                inst.error_detail = "worker disconnected"
        logger.info("worker marked disconnected", worker_id=str(worker.id))
    if stale:
        await db.commit()
