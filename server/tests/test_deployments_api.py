"""Deployment group API tests: deploy replicas, scale, delete, 404s."""

from __future__ import annotations

import uuid

import pytest
from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from inferna_server.models import Cluster, Deployment, Model, ModelInstance


async def _admin_token(client) -> str:
    return await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])


async def _model_id(db, name: str) -> str:
    model = (await db.execute(select(Model).where(Model.name == name))).scalar_one()
    return str(model.id)


async def _cluster_id(db) -> str:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    return str(cluster.id)


def _deploy_body(model_id: str, cluster_id: str, **overrides) -> dict:
    body = {
        "model_id": model_id,
        "cluster_id": cluster_id,
        "engine": "vllm",
        "profile": "latency",
        "gpu_selection": "auto",
    }
    body.update(overrides)
    return body


async def _deploy(client, token: str, db, *, replicas: int = 1) -> list[dict]:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name=f"w-r{replicas}")
    resp = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(
            await _model_id(db, "Qwen/Qwen2.5-0.5B-Instruct"),
            str(cluster.id),
            replicas=replicas,
        ),
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_deploy_replicas_returns_list_and_creates_group(client, db) -> None:
    token = await _admin_token(client)
    body = await _deploy(client, token, db, replicas=3)

    assert isinstance(body, list) and len(body) == 3
    deployment_ids = {instance["deployment_id"] for instance in body}
    assert len(deployment_ids) == 1
    assert {instance["state"] for instance in body} == {"scheduled"}
    assert {instance["desired_state"] for instance in body} == {"running"}
    assert len({instance["port"] for instance in body}) == 3

    listing = await client.get("/api/v1/deployments", headers=auth_headers(token))
    assert listing.status_code == 200
    groups = listing.json()
    assert len(groups) == 1
    group = groups[0]
    assert str(group["id"]) == deployment_ids.pop()
    assert group["min_replicas"] == 3 and group["max_replicas"] == 3
    assert len(group["instances"]) == 3
    assert group["model"]["display_name"] == "Qwen2.5 0.5B Instruct"


async def test_deploy_manual_rejects_multiple_replicas(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    worker = await add_connected_worker(db, cluster.id, name="manual")
    resp = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(
            await _model_id(db, "Qwen/Qwen2.5-0.5B-Instruct"),
            str(cluster.id),
            replicas=3,
            gpu_selection={"worker_id": str(worker.id), "gpu_indexes": [0]},
        ),
        headers=auth_headers(token),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "manual gpu selection supports a single replica"


async def test_scale_up_and_down_updates_live_replicas(client, db) -> None:
    token = await _admin_token(client)
    deployed = await _deploy(client, token, db, replicas=1)
    dep_id = deployed[0]["deployment_id"]

    up = await client.post(
        f"/api/v1/deployments/{dep_id}/scale",
        json={"replicas": 3},
        headers=auth_headers(token),
    )
    assert up.status_code == 200, up.text
    scaled = up.json()
    assert scaled["min_replicas"] == 3 and scaled["max_replicas"] == 3
    live = [i for i in scaled["instances"] if i["desired_state"] == "running"]
    assert len(live) == 3

    row = (await db.execute(select(Deployment).where(Deployment.id == uuid.UUID(dep_id)))).scalar_one()
    assert row.last_scaled_at is not None
    first_scaled_at = row.last_scaled_at

    down = await client.post(
        f"/api/v1/deployments/{dep_id}/scale",
        json={"replicas": 1},
        headers=auth_headers(token),
    )
    assert down.status_code == 200
    instances = down.json()["instances"]
    running = [i for i in instances if i["desired_state"] == "running"]
    stopped = [i for i in instances if i["desired_state"] == "stopped"]
    assert len(running) == 1 and len(stopped) == 2
    # Scale-in bumps generation so the worker receives the stop command.
    assert all(i["generation"] >= 2 for i in stopped)

    await db.refresh(row)
    assert row.last_scaled_at is not None
    assert row.last_scaled_at >= first_scaled_at


async def test_scale_in_prefers_non_running_replica(client, db) -> None:
    token = await _admin_token(client)
    deployed = await _deploy(client, token, db, replicas=3)
    dep_id = uuid.UUID(deployed[0]["deployment_id"])

    # Push one replica into the error state directly; desired_state stays "running".
    instances = (
        await db.execute(select(ModelInstance).where(ModelInstance.deployment_id == dep_id))
    ).scalars().all()
    assert len(instances) == 3
    broken = instances[0]
    broken.state = "error"
    broken.error_detail = "boom"
    await db.commit()

    resp = await client.post(
        f"/api/v1/deployments/{dep_id}/scale",
        json={"replicas": 2},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    by_id = {str(i["id"]): i for i in resp.json()["instances"]}

    stopped = by_id[str(broken.id)]
    assert stopped["desired_state"] == "stopped"
    assert stopped["generation"] == 2
    assert stopped["error_detail"] is None
    untouched = [i for key, i in by_id.items() if key != str(broken.id)]
    assert all(
        i["desired_state"] == "running" and i["generation"] == 1 for i in untouched
    )

    row = (await db.execute(select(Deployment).where(Deployment.id == dep_id))).scalar_one()
    await db.refresh(row)
    assert row.last_scaled_at is not None


async def test_scale_replaces_errored_replicas_without_counting_them(client, db) -> None:
    """Greptile P1: errored replicas hold a port/GPU slot but serve nothing, so
    they must not count toward the target capacity.

    3 replicas (1 running, 2 error), scale to 2: both broken records are
    stopped in place (generation bump, error cleared, NOT deleted), the healthy
    replica is untouched, and one fresh scheduled record is allocated so
    desired-running returns to 2 — serving now: 1, after worker start: 2.
    """
    token = await _admin_token(client)
    deployed = await _deploy(client, token, db, replicas=3)
    dep_id = uuid.UUID(deployed[0]["deployment_id"])

    instances = (
        await db.execute(select(ModelInstance).where(ModelInstance.deployment_id == dep_id))
    ).scalars().all()
    assert len(instances) == 3
    healthy = instances[0]
    broken_one, broken_two = instances[1], instances[2]
    # Deploy seeds replicas as scheduled; promote the healthy one so the group
    # matches the reviewed scenario exactly: 1 running + 2 error.
    healthy.state = "running"
    for broken in (broken_one, broken_two):
        broken.state = "error"
        broken.error_detail = "oom"
    await db.commit()

    resp = await client.post(
        f"/api/v1/deployments/{dep_id}/scale",
        json={"replicas": 2},
        headers=auth_headers(token),
    )
    by_id = {str(i["id"]): i for i in resp.json()["instances"]}

    # Broken records stopped in place so the worker frees their port/GPU slot;
    # observed state stays "error" until the worker reports the stop.
    for broken in (broken_one, broken_two):
        row = by_id[str(broken.id)]
        assert row["desired_state"] == "stopped"
        assert row["generation"] == 2
        assert row["error_detail"] is None
    # The healthy replica is untouched.
    kept = by_id[str(healthy.id)]
    assert kept["desired_state"] == "running" and kept["state"] == "running"
    assert kept["generation"] == 1
    # The endpoint committed in its own session; expire this session's cached
    # copies of the original replicas or the re-read returns stale attributes.
    db.expire_all()

    rows = (
        await db.execute(select(ModelInstance).where(ModelInstance.deployment_id == dep_id))
    ).scalars().all()
    original_ids = {healthy.id, broken_one.id, broken_two.id}
    replacements = [r for r in rows if r.id not in original_ids]
    assert len(rows) == 4 and len(replacements) == 1
    repl = replacements[0]
    # The replacement went through allocate_replicas: a fresh scheduled record.
    assert repl.state == "scheduled" and repl.desired_state == "running"
    assert repl.generation == 1 and repl.port is not None

    desired_running = [r for r in rows if r.desired_state == "running"]
    assert len(desired_running) == 2
    serving_now = [r for r in desired_running if r.state == "running"]
    pending = [r for r in desired_running if r.state != "running"]
    assert len(serving_now) == 1 and len(pending) == 1


async def test_delete_group_cascades_to_replicas(client, db) -> None:
    token = await _admin_token(client)
    deployed = await _deploy(client, token, db, replicas=2)
    dep_id = deployed[0]["deployment_id"]

    resp = await client.delete(f"/api/v1/deployments/{dep_id}", headers=auth_headers(token))
    assert resp.status_code == 204

    instances = await client.get("/api/v1/model-instances", headers=auth_headers(token))
    assert instances.json() == []
    groups = await client.get("/api/v1/deployments", headers=auth_headers(token))
    assert groups.json() == []
    remaining = (
        await db.execute(
            select(func.count())
            .select_from(ModelInstance)
            .where(ModelInstance.deployment_id == uuid.UUID(dep_id))
        )
    ).scalar_one()
    assert remaining == 0


async def test_scale_unknown_deployment_404(client) -> None:
    token = await _admin_token(client)
    missing = uuid.uuid4()
    resp = await client.post(
        f"/api/v1/deployments/{missing}/scale",
        json={"replicas": 2},
        headers=auth_headers(token),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "deployment not found"


async def test_delete_unknown_deployment_404(client) -> None:
    token = await _admin_token(client)
    resp = await client.delete(
        f"/api/v1/deployments/{uuid.uuid4()}", headers=auth_headers(token)
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "deployment not found"


async def test_requires_auth(client) -> None:
    resp = await client.get("/api/v1/deployments")
    assert resp.status_code == 401
    resp = await client.post(
        "/api/v1/deployments/00000000-0000-0000-0000-000000000000/scale",
        json={"replicas": 1},
    )
    assert resp.status_code == 401
    resp = await client.delete("/api/v1/deployments/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 401


async def test_deployment_max_replicas_check_constraint(db) -> None:

    model_id = (
        await db.execute(select(Model.id).where(Model.name == "Qwen/Qwen2.5-0.5B-Instruct"))
    ).scalar_one()
    cluster_id = (await db.execute(select(Cluster.id).where(Cluster.name == "default"))).scalar_one()

    def _deployment(max_replicas: int) -> Deployment:
        return Deployment(
            model_id=model_id,
            cluster_id=cluster_id,
            engine="vllm",
            profile="latency",
            min_replicas=1,
            max_replicas=max_replicas,
        )

    db.add(_deployment(max_replicas=9))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    db.add(_deployment(max_replicas=8))
    await db.commit()
