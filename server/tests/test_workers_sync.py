"""gRPC register/sync service-layer tests (register → sync → commands round trip)."""

from __future__ import annotations

import uuid

import grpc
import pytest
from sqlalchemy import select

from inferna_server.models import Cluster, Model, ModelInstance, Worker, WorkerGPU
from inferna_server.proto import cluster_pb2
from inferna_server.services.workers_svc import register_worker, sync_worker

REG_TOKEN = "inferna-registration-token"


def make_register(
    token: str = REG_TOKEN,
    hostname: str = "h1",
    worker_name: str = "",
    cluster_name: str = "default",
    version: str = "0.1.0",
) -> cluster_pb2.RegisterRequest:
    return cluster_pb2.RegisterRequest(
        cluster_token=token,
        hostname=hostname,
        worker_name=worker_name,
        cluster_name=cluster_name,
        version=version,
    )


def gpu(index: int = 0, vram: int = 24576) -> cluster_pb2.GPUInfo:
    return cluster_pb2.GPUInfo(
        index=index,
        vendor="mock",
        name="Mock GPU A",
        vram_mb=vram,
        used_vram_mb=0,
        utilization_pct=0,
        uuid=f"gpu-{index}",
        driver_version="1.0",
    )


def status(instance_id: str, state: str = "running", port: int = 0) -> cluster_pb2.InstanceStatus:
    return cluster_pb2.InstanceStatus(instance_id=instance_id, state=state, port=port)


def make_sync(
    worker_id: str,
    worker_token: str,
    gpus: list[cluster_pb2.GPUInfo] | None = None,
    instances: list[cluster_pb2.InstanceStatus] | None = None,
) -> cluster_pb2.SyncRequest:
    return cluster_pb2.SyncRequest(
        worker_id=worker_id,
        worker_token=worker_token,
        system=cluster_pb2.SystemInfo(cpu_cores=8, memory_mb=16384, os="linux", hostname="h1"),
        gpus=gpus or [],
        instances=instances or [],
    )


async def _register(db) -> tuple[str, str]:
    resp = await register_worker(db, make_register())
    return resp.worker_id, resp.worker_token


async def _deploy_scheduled(db, worker_id: str, port: int = 8010) -> str:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = (
        await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-7B-Instruct"))
    ).scalar_one()
    instance = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        worker_id=uuid.UUID(worker_id),
        engine="vllm",
        profile="latency",
        gpu_indexes=[0],
        state="scheduled",
        port=port,
    )
    db.add(instance)
    await db.commit()
    return str(instance.id)


async def test_register_wrong_token_rejected(db) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await register_worker(db, make_register(token="nope"))
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_register_unknown_cluster_rejected(db) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await register_worker(db, make_register(cluster_name="missing"))
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


async def test_register_returns_id_token_interval(db) -> None:
    resp = await register_worker(db, make_register(worker_name="nice-name"))
    assert resp.worker_id
    assert len(resp.worker_token) == 32
    assert resp.sync_interval_seconds == 5
    worker = await db.get(Worker, uuid.UUID(resp.worker_id))
    assert worker is not None
    assert worker.name == "nice-name"
    assert worker.hostname == "h1"


async def test_register_reuses_worker_by_hostname_and_rotates_token(db) -> None:
    first = await register_worker(db, make_register())
    second = await register_worker(db, make_register())
    assert first.worker_id == second.worker_id
    assert first.worker_token != second.worker_token


async def test_sync_wrong_token_rejected(db) -> None:
    worker_id, _ = await _register(db)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await sync_worker(db, make_sync(worker_id, "wrong"))
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_sync_upserts_gpus_and_removes_stale(db) -> None:
    worker_id, token = await _register(db)
    resp = await sync_worker(db, make_sync(worker_id, token, gpus=[gpu(0), gpu(1, vram=49152)]))
    assert not resp.commands
    rows = (
        (await db.execute(select(WorkerGPU).where(WorkerGPU.worker_id == uuid.UUID(worker_id))))
        .scalars()
        .all()
    )
    assert {r.index for r in rows} == {0, 1}
    assert {r.vram_mb for r in rows} == {24576, 49152}

    await sync_worker(db, make_sync(worker_id, token, gpus=[gpu(0)]))
    rows = (
        (await db.execute(select(WorkerGPU).where(WorkerGPU.worker_id == uuid.UUID(worker_id))))
        .scalars()
        .all()
    )
    assert [r.index for r in rows] == [0]


async def test_sync_marks_worker_connected_with_system_info(db) -> None:
    worker_id, token = await _register(db)
    await sync_worker(db, make_sync(worker_id, token, gpus=[gpu(0)]))
    worker = await db.get(Worker, uuid.UUID(worker_id))
    assert worker.state == "connected"
    assert worker.cpu_cores == 8
    assert worker.memory_mb == 16384
    assert worker.os == "linux"


async def test_start_command_for_scheduled_instance(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    resp = await sync_worker(db, make_sync(worker_id, token))
    assert len(resp.commands) == 1
    command = resp.commands[0]
    assert command.instance_id == instance_id
    assert command.action == "start"
    assert command.config.engine == "vllm"
    assert command.config.model_name == "Qwen/Qwen2.5-7B-Instruct"
    assert command.config.profile == "latency"
    assert command.config.gpu_indexes == [0]
    assert command.config.vram_required_mb == 16384
    assert command.config.port == 8010


async def test_worker_report_persists_running_state(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running", port=8010)])
    )
    assert not resp.commands  # scheduled -> worker will start it; nothing else to do
    instance = await db.get(ModelInstance, uuid.UUID(instance_id))
    assert instance.state == "running"
    assert instance.port == 8010


async def test_stop_command_when_worker_still_running(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    await sync_worker(db, make_sync(worker_id, token, instances=[status(instance_id, "running")]))
    instance = await db.get(ModelInstance, uuid.UUID(instance_id))
    instance.state = "stopped"
    await db.commit()
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running")])
    )
    actions = [(c.instance_id, c.action) for c in resp.commands]
    assert (instance_id, "stop") in actions


async def test_desired_stop_not_overwritten_by_worker_report(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    instance = await db.get(ModelInstance, uuid.UUID(instance_id))
    instance.state = "stopped"
    await db.commit()
    await sync_worker(db, make_sync(worker_id, token, instances=[status(instance_id, "running")]))
    instance = await db.get(ModelInstance, uuid.UUID(instance_id))
    assert instance.state == "stopped"


async def test_delete_command_for_orphan_container(db) -> None:
    worker_id, token = await _register(db)
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status("missing-id", "running")])
    )
    actions = [(c.instance_id, c.action) for c in resp.commands]
    assert ("missing-id", "delete") in actions
