"""Instance log snapshots: sync persistence (incl. the size cap) and the logs endpoint."""

from __future__ import annotations

import asyncio
import uuid

from conftest import TEST_ADMIN, auth_headers, login
from sqlalchemy import select

from inferna_server.models import Cluster, Deployment, Model, ModelInstance
from inferna_server.proto import cluster_pb2
from inferna_server.services.workers_svc import LOG_TAIL_MAX_CHARS, register_worker, sync_worker

REG_TOKEN = "inferna-registration-token"


def _register_request(hostname: str = "h-logs") -> cluster_pb2.RegisterRequest:
    return cluster_pb2.RegisterRequest(
        cluster_token=REG_TOKEN,
        hostname=hostname,
        worker_name="",
        cluster_name="default",
        version="0.2.0",
        protocol_version=1,
        address="",
    )


def _sync(
    worker_id: str, worker_token: str, instances: list[cluster_pb2.InstanceStatus]
) -> cluster_pb2.SyncRequest:
    return cluster_pb2.SyncRequest(
        worker_id=worker_id,
        worker_token=worker_token,
        system=cluster_pb2.SystemInfo(cpu_cores=4, memory_mb=8192, os="linux", hostname="h-logs"),
        gpus=[
            cluster_pb2.GPUInfo(
                index=0,
                vendor="mock",
                name="Mock GPU A",
                vram_mb=24576,
                used_vram_mb=0,
                utilization_pct=0,
                uuid="",
                driver_version="",
            )
        ],
        instances=instances,
    )


async def _worker(db) -> tuple[str, str]:
    resp = await register_worker(db, _register_request())
    return resp.worker_id, resp.worker_token


async def _scheduled_instance(db, worker_id: str) -> ModelInstance:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = (
        await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-7B-Instruct"))
    ).scalar_one()
    deployment = Deployment(
        model_id=model.id, cluster_id=cluster.id, engine="vllm", profile="latency"
    )
    db.add(deployment)
    await db.flush()
    instance = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        worker_id=uuid.UUID(worker_id),
        deployment=deployment,
        engine="vllm",
        profile="latency",
        gpu_indexes=[0],
        state="scheduled",
        desired_state="running",
        generation=1,
        port=8010,
    )
    db.add(instance)
    await db.commit()
    return instance


def _status(instance_id: str, **overrides) -> cluster_pb2.InstanceStatus:
    payload = {"instance_id": instance_id, "state": "running", "port": 8010, "generation": 1}
    payload.update(overrides)
    return cluster_pb2.InstanceStatus(**payload)


# --- sync → database ---


async def test_sync_persists_log_tail(db) -> None:
    worker_id, token = await _worker(db)
    instance = await _scheduled_instance(db, worker_id)

    await sync_worker(
        db, _sync(worker_id, token, [_status(str(instance.id), log_tail="engine: boot ok\nengine: ready")])
    )

    await db.refresh(instance)
    assert instance.state == "running"
    assert instance.log_tail == "engine: boot ok\nengine: ready"


async def test_sync_keeps_previous_tail_when_worker_reports_empty(db) -> None:
    worker_id, token = await _worker(db)
    instance = await _scheduled_instance(db, worker_id)

    await sync_worker(db, _sync(worker_id, token, [_status(str(instance.id), log_tail="first")]))
    await sync_worker(db, _sync(worker_id, token, [_status(str(instance.id), log_tail="")]))

    await db.refresh(instance)
    assert instance.log_tail == "first"


async def test_sync_truncates_oversized_tail_keeping_the_newest_bytes(db) -> None:
    worker_id, token = await _worker(db)
    instance = await _scheduled_instance(db, worker_id)
    oversized = "a" * 4000 + "z" * 5000  # 9000 chars > LOG_TAIL_MAX_CHARS

    await sync_worker(db, _sync(worker_id, token, [_status(str(instance.id), log_tail=oversized)]))

    await db.refresh(instance)
    assert instance.log_tail is not None
    assert len(instance.log_tail) == LOG_TAIL_MAX_CHARS
    assert instance.log_tail == oversized[-LOG_TAIL_MAX_CHARS:]


# --- read endpoint ---


async def _instance_row(db, deployment_factory, *, log_tail: str | None) -> ModelInstance:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = (
        await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-7B-Instruct"))
    ).scalar_one()
    deployment = await deployment_factory(model.id, cluster.id)
    instance = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        deployment_id=deployment.id,
        engine="vllm",
        profile="latency",
        gpu_indexes=[0],
        state="error",
        desired_state="running",
        generation=1,
        port=8010,
        error_detail="health probe timed out",
        log_tail=log_tail,
    )
    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    return instance


async def test_logs_endpoint_returns_snapshot(client, db, deployment_factory) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    instance = await _instance_row(db, deployment_factory, log_tail="vllm: CUDA out of memory")

    resp = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs", headers=auth_headers(token)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance_id"] == str(instance.id)
    assert body["state"] == "error"
    assert body["log_tail"] == "vllm: CUDA out of memory"
    assert body["updated_at"]


async def test_logs_endpoint_handles_missing_snapshot(client, db, deployment_factory) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    instance = await _instance_row(db, deployment_factory, log_tail=None)

    resp = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs", headers=auth_headers(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["log_tail"] is None


async def test_logs_endpoint_unknown_instance_and_auth(client, db, deployment_factory) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    missing = uuid.uuid4()

    unauthorized = await client.get(f"/api/v1/model-instances/{missing}/logs")
    not_found = await client.get(
        f"/api/v1/model-instances/{missing}/logs", headers=auth_headers(token)
    )

    assert unauthorized.status_code == 401
    assert not_found.status_code == 404
    assert not_found.json()["detail"] == "instance not found"


# --- live refresh (the pull-based equivalent of "fetch logs now") ---


async def test_live_logs_refresh_from_the_next_sync(client, db, deployment_factory, monkeypatch) -> None:
    """`live=true` waits for the worker's next Sync, because the server never dials a GPU host."""
    from inferna_server.api import instances as instances_api
    from inferna_server.services.workers_svc import sync_worker

    monkeypatch.setattr(instances_api, "LIVE_LOG_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(instances_api, "LIVE_LOG_POLL_SECONDS", 0.05)
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    worker_id, worker_token = await _worker(db)
    instance = await _scheduled_instance(db, worker_id)
    instance.log_tail = "old line"
    await db.commit()

    async def refresh_soon() -> None:
        # A Sync is what "live" waits for; it touches the worker heartbeat even when the row's
        # fields do not change (that is why freshness is not measured on `updated_at`).
        await asyncio.sleep(0.2)
        await sync_worker(
            db, _sync(worker_id, worker_token, [_status(str(instance.id), log_tail="fresh line")])
        )

    task = asyncio.create_task(refresh_soon())
    resp = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs?live=true", headers=auth_headers(token)
    )
    await task

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["log_tail"] == "fresh line"
    assert body["stale"] is False


async def test_live_logs_report_stale_instead_of_failing(client, db, deployment_factory, monkeypatch) -> None:
    from inferna_server.api import instances as instances_api

    monkeypatch.setattr(instances_api, "LIVE_LOG_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(instances_api, "LIVE_LOG_POLL_SECONDS", 0.05)
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    instance = await _instance_row(db, deployment_factory, log_tail="old line")

    resp = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs?live=true", headers=auth_headers(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["stale"] is True
    assert resp.json()["log_tail"] == "old line"


async def test_log_lines_are_capped_on_request(client, db, deployment_factory) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    instance = await _instance_row(
        db, deployment_factory, log_tail="\n".join(f"line {index}" for index in range(50))
    )

    resp = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs?lines=5", headers=auth_headers(token)
    )
    too_many = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs?lines=5000", headers=auth_headers(token)
    )

    assert resp.status_code == 200
    assert resp.json()["log_tail"].splitlines() == [f"line {index}" for index in range(45, 50)]
    assert resp.json()["lines"] == 5
    assert too_many.status_code == 422


async def test_missing_snapshot_stays_none_with_line_capping(client, db, deployment_factory) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    instance = await _instance_row(db, deployment_factory, log_tail=None)

    resp = await client.get(
        f"/api/v1/model-instances/{instance.id}/logs?lines=10", headers=auth_headers(token)
    )

    assert resp.status_code == 200
    assert resp.json()["log_tail"] is None  # "no logs yet" is not an empty string
