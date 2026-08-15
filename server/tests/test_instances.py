"""Instance lifecycle API tests: deploy (auto/manual), stop, delete, dashboard."""

from __future__ import annotations

from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from sqlalchemy import select

from inferna_server.models import Cluster, Model


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


async def test_deploy_happy_path(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="w1", gpus=((0, "mock", "Mock GPU A", 24576),))
    resp = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(await _model_id(db, "Qwen/Qwen2.5-7B-Instruct"), str(cluster.id)),
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "scheduled"
    assert body["port"] == 8010
    assert body["engine"] == "vllm"
    assert body["profile"] == "latency"
    assert body["gpu_indexes"] == [0]
    assert body["worker_name"] == "w1"


async def test_deploy_no_fit_400(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="small", gpus=((0, "mock", "A", 8192),))
    resp = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(await _model_id(db, "Qwen/Qwen2.5-7B-Instruct"), str(cluster.id)),
        headers=auth_headers(token),
    )
    assert resp.status_code == 400
    assert "no GPU with enough free VRAM" in resp.json()["detail"]


async def test_deploy_manual_selection(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    worker = await add_connected_worker(
        db, cluster.id, name="manual", gpus=((0, "mock", "A", 24576), (1, "mock", "B", 24576))
    )
    resp = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(
            await _model_id(db, "Qwen/Qwen2.5-0.5B-Instruct"),
            str(cluster.id),
            gpu_selection={"worker_id": str(worker.id), "gpu_indexes": [1]},
        ),
        headers=auth_headers(token),
    )
    assert resp.status_code == 201
    assert resp.json()["gpu_indexes"] == [1]
    assert resp.json()["worker_name"] == "manual"


async def test_deploy_unknown_model_404(client, db) -> None:
    token = await _admin_token(client)
    resp = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body("00000000-0000-0000-0000-000000000099", await _cluster_id(db)),
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


async def test_list_instances_includes_names(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="listed", gpus=((0, "mock", "A", 24576),))
    await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(await _model_id(db, "Qwen/Qwen2.5-0.5B-Instruct"), str(cluster.id)),
        headers=auth_headers(token),
    )
    resp = await client.get("/api/v1/model-instances", headers=auth_headers(token))
    assert resp.status_code == 200
    instance = resp.json()[0]
    assert instance["model"]["display_name"] == "Qwen2.5 0.5B Instruct"
    assert instance["worker_name"] == "listed"


async def test_stop_instance(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="st", gpus=((0, "mock", "A", 24576),))
    created = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(await _model_id(db, "Qwen/Qwen2.5-0.5B-Instruct"), str(cluster.id)),
        headers=auth_headers(token),
    )
    instance_id = created.json()["id"]
    resp = await client.post(
        f"/api/v1/model-instances/{instance_id}/stop", headers=auth_headers(token)
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "stopped"


async def test_delete_instance(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="del", gpus=((0, "mock", "A", 24576),))
    created = await client.post(
        "/api/v1/model-instances",
        json=_deploy_body(await _model_id(db, "Qwen/Qwen2.5-0.5B-Instruct"), str(cluster.id)),
        headers=auth_headers(token),
    )
    instance_id = created.json()["id"]
    resp = await client.delete(
        f"/api/v1/model-instances/{instance_id}", headers=auth_headers(token)
    )
    assert resp.status_code == 204
    listing = await client.get("/api/v1/model-instances", headers=auth_headers(token))
    assert listing.json() == []


async def test_dashboard_numbers(client, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="dash", gpus=((0, "mock", "A", 24576),))
    resp = await client.get("/api/v1/dashboard", headers=auth_headers(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["clusters"] == 1
    assert body["workers_online"] == 1
    assert body["gpus_total"] == 1
    assert body["vram_total_mb"] == 24576
    assert body["vram_used_mb"] == 0
    assert body["instances_running"] == 0


async def test_requires_auth(client) -> None:
    resp = await client.get("/api/v1/model-instances")
    assert resp.status_code == 401
