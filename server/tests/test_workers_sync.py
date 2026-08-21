"""gRPC register/sync service-layer tests (register → sync → commands round trip)."""

from __future__ import annotations

import uuid

import grpc
import pytest
from sqlalchemy import select

from inferna_server.models import Cluster, Model, ModelInstance
from inferna_server.proto import cluster_pb2
from inferna_server.services.workers_svc import mark_disconnected, register_worker, sync_worker

REG_TOKEN = "inferna-registration-token"


def make_register(
    token: str = REG_TOKEN,
    hostname: str = "h1",
    worker_name: str = "",
    cluster_name: str = "default",
    version: str = "0.2.0",
    protocol_version: int = 1,
) -> cluster_pb2.RegisterRequest:
    return cluster_pb2.RegisterRequest(
        cluster_token=token,
        hostname=hostname,
        worker_name=worker_name,
        cluster_name=cluster_name,
        version=version,
        protocol_version=protocol_version,
    )


def gpu(index: int = 0, vram: int = 24576) -> cluster_pb2.GPUInfo:
    return cluster_pb2.GPUInfo(
        index=index,
        vendor="mock",
        name="Mock GPU A",
        vram_mb=vram,
        used_vram_mb=0,
        utilization_pct=0,
        uuid="",
        driver_version="",
    )


def status(instance_id: str, state: str = "running", port: int = 0, generation: int = 0) -> cluster_pb2.InstanceStatus:
    return cluster_pb2.InstanceStatus(instance_id=instance_id, state=state, port=port, generation=generation)


def make_sync(
    worker_id: str, worker_token: str, gpus: list[cluster_pb2.GPUInfo] | None = None, instances: list[cluster_pb2.InstanceStatus] | None = None
) -> cluster_pb2.SyncRequest:
    return cluster_pb2.SyncRequest(
        worker_id=worker_id,
        worker_token=worker_token,
        system=cluster_pb2.SystemInfo(cpu_cores=4, memory_mb=8192, os="linux", hostname="h1"),
        gpus=gpus or [gpu()],
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
        desired_state="running",
        generation=1,
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
    assert resp.worker_token
    assert resp.sync_interval_seconds == 5
    from sqlalchemy import select as ssel

    worker = (await db.execute(select(ModelInstance))).scalar_one_or_none()
    # check worker persisted
    from inferna_server.models import Worker

    worker = (await db.execute(select(Worker).where(Worker.hostname == "h1"))).scalar_one()
    assert worker.hostname == "h1"


async def test_register_reuses_worker_by_hostname_and_rotates_token(db) -> None:
    first = await register_worker(db, make_register())
    second = await register_worker(db, make_register())
    assert first.worker_id == second.worker_id
    assert first.worker_token != second.worker_token


async def test_sync_wrong_token_rejected(db) -> None:
    worker_id, _ = await _register(db)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await sync_worker(db, make_sync(worker_id, "bad"))
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_sync_upserts_gpus_and_removes_stale(db) -> None:
    worker_id, token = await _register(db)
    await sync_worker(db, make_sync(worker_id, token, gpus=[gpu(0), gpu(1)]))
    await sync_worker(db, make_sync(worker_id, token, gpus=[gpu(0)]))
    from inferna_server.models import WorkerGPU

    rows = (await db.execute(select(WorkerGPU).where(WorkerGPU.worker_id == uuid.UUID(worker_id)))).scalars().all()
    assert [r.index for r in rows] == [0]


async def test_sync_marks_worker_connected_with_system_info(db) -> None:
    worker_id, token = await _register(db)
    await sync_worker(db, make_sync(worker_id, token))
    from inferna_server.models import Worker

    worker = (await db.execute(select(Worker).where(Worker.id == uuid.UUID(worker_id)))).scalar_one()
    assert worker.state == "connected"
    assert worker.os == "linux"


async def test_start_command_for_scheduled_instance(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    resp = await sync_worker(db, make_sync(worker_id, token))
    actions = [(c.instance_id, c.action, c.generation) for c in resp.commands]
    assert (instance_id, "start", 1) in actions
    command = next(c for c in resp.commands if c.instance_id == instance_id)
    assert command.config.port == 8010


async def test_worker_report_persists_running_state(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # worker reports running with correct generation -> server persists observed state, no further commands
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running", port=8010, generation=1)])
    )
    # First sync after deploy should still send start? No, because reported generation 1 matches desired 1, so no start needed now but instance state should be updated
    # Instead test that second sync with same report yields 0 commands (no-op)
    # Let's do second sync with same report
    resp2 = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running", port=8010, generation=1)])
    )
    assert not resp2.commands
    instance = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    assert instance.state == "running"
    assert instance.port == 8010


async def test_no_duplicate_start_when_generation_matches(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # report running with generation == desired -> 0 commands
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running", port=8010, generation=1)])
    )
    # The first report already had applied==generation, but obs is running not stopped, so no start.
    # Actually initial sync before report would have sent start; now with report generation 1, no further start.
    resp2 = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running", port=8010, generation=1)])
    )
    assert len(resp2.commands) == 0


async def test_start_resent_when_generation_mismatch(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # old generation 0 < 1 -> should resend start with generation 1
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "running", port=8010, generation=0)])
    )
    assert len([c for c in resp.commands if c.action == "start" and c.generation == 1]) == 1


async def test_start_when_no_report(db) -> None:
    worker_id, token = await _register(db)
    await _deploy_scheduled(db, worker_id)
    resp = await sync_worker(db, make_sync(worker_id, token, instances=[]))
    assert any(c.action == "start" for c in resp.commands)


async def test_no_restart_on_error_without_retry(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # worker reports error with same generation -> no auto-restart
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "error", generation=1)])
    )
    assert len([c for c in resp.commands if c.instance_id == instance_id]) == 0
    # also check state persisted to error
    inst = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    assert inst.state == "error"


async def test_restart_on_stopped_with_matching_generation(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # first mark as running with gen 1
    await sync_worker(db, make_sync(worker_id, token, instances=[status(instance_id, "running", generation=1)]))
    # now worker reports stopped but generation matches -> should trigger start (recovery)
    resp = await sync_worker(
        db, make_sync(worker_id, token, instances=[status(instance_id, "stopped", generation=1)])
    )
    assert any(c.instance_id == instance_id and c.action == "start" and c.generation == 1 for c in resp.commands)


async def test_stop_command_when_desired_stopped_and_worker_running(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # Mark desired stopped (simulate user stop)
    inst = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    inst.desired_state = "stopped"
    inst.generation += 1
    await db.commit()
    # worker still reports running
    resp = await sync_worker(db, make_sync(worker_id, token, instances=[status(instance_id, "running", generation=1)]))
    actions = [(c.instance_id, c.action) for c in resp.commands]
    assert (instance_id, "stop") in actions


async def test_observed_follows_worker_after_stop(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # request stop (desired stopped)
    inst = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    inst.desired_state = "stopped"
    inst.generation += 1
    await db.commit()
    # worker reports running then next sync worker reports stopped? Actually sync should accept report even when desired stopped.
    # The sync after stop request where worker still reports running should get stop command, then worker executes and reports stopped.
    resp = await sync_worker(db, make_sync(worker_id, token, instances=[status(instance_id, "running", generation=1)]))
    assert any(c.action == "stop" for c in resp.commands)
    # Now worker reports stopped with generation maybe still 1 (old) - should be accepted as observed
    resp2 = await sync_worker(db, make_sync(worker_id, token, instances=[status(instance_id, "stopped", generation=1)]))
    inst2 = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    assert inst2.state == "stopped"
    # No further stop commands when already stopped
    assert not any(c.action == "stop" for c in resp2.commands)


async def test_delete_command_for_orphan_container(db) -> None:
    worker_id, token = await _register(db)
    resp = await sync_worker(db, make_sync(worker_id, token, instances=[status("missing-id", "running")]))
    actions = [(c.instance_id, c.action) for c in resp.commands]
    assert ("missing-id", "delete") in actions


async def test_mark_disconnected_ignores_stopped_desired(db) -> None:
    worker_id, token = await _register(db)
    instance_id = await _deploy_scheduled(db, worker_id)
    # set desired stopped
    inst = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    inst.desired_state = "stopped"
    inst.generation += 1
    inst.state = "stopped"
    await db.commit()
    # make worker stale
    from datetime import datetime, timedelta, timezone

    from inferna_server.models import Worker

    worker = (await db.execute(select(Worker).where(Worker.id == uuid.UUID(worker_id)))).scalar_one()
    worker.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    await db.commit()
    await mark_disconnected(db)
    inst_after = (await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(instance_id)))).scalar_one()
    # should remain stopped, not error
    assert inst_after.state == "stopped"
    assert worker.state == "disconnected"
