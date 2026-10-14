"""Custom model catalog: admin-only creation rules and deletion guards."""

from __future__ import annotations

import uuid

from conftest import TEST_ADMIN, auth_headers, login
from sqlalchemy import select

from inferna_server.auth import hash_password
from inferna_server.models import Cluster, Model, User
from inferna_server.services.workers_svc import seed_catalog

NEW_MODEL = {
    "hf_repo_id": "Qwen/Qwen2.5-3B-Instruct",
    "category": "llm",
    "vram_required_mb": 8192,
    "requires_hf_token": False,
}


async def _add_user(db, username: str = "alice", role: str = "user") -> uuid.UUID:
    user = User(
        username=username,
        password_hash=hash_password("secret"),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user.id


async def test_create_custom_model_derives_engines(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])

    resp = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Qwen/Qwen2.5-3B-Instruct"
    assert body["display_name"] == "Qwen/Qwen2.5-3B-Instruct"
    assert body["is_builtin"] is False
    assert body["supported_engines"] == ["vllm", "sglang"]
    assert body["params_b"] is None
    assert body["vram_required_mb"] == 8192


async def test_create_derives_engines_per_category(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])

    reranker = await client.post(
        "/api/v1/models",
        json={**NEW_MODEL, "hf_repo_id": "BAAI/bge-reranker-v2", "category": "reranker"},
        headers=auth_headers(token),
    )
    audio = await client.post(
        "/api/v1/models",
        json={**NEW_MODEL, "hf_repo_id": "acme/whisper-large-v3", "category": "audio"},
        headers=auth_headers(token),
    )

    assert reranker.status_code == 201, reranker.text
    assert reranker.json()["supported_engines"] == ["sglang"]
    assert audio.status_code == 201, audio.text
    assert audio.json()["supported_engines"] == []


async def test_create_rejects_builtin_and_duplicate_names(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    builtin = (
        await db.execute(select(Model).where(Model.is_builtin.is_(True)).limit(1))
    ).scalar_one()

    reserved = await client.post(
        "/api/v1/models", json={**NEW_MODEL, "hf_repo_id": builtin.name}, headers=auth_headers(token)
    )
    first = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))
    duplicate = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))

    assert reserved.status_code == 409
    assert reserved.json()["detail"] == "name is reserved by a builtin model"
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "model already exists"


async def test_create_is_admin_only(client, db) -> None:
    await _add_user(db)
    token = await login(client, "alice", "secret")

    resp = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin privileges required"


async def test_create_validates_input(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])

    cases = [
        {**NEW_MODEL, "category": "video"},
        {**NEW_MODEL, "vram_required_mb": 0},
        {**NEW_MODEL, "hf_repo_id": "ab"},
        {**NEW_MODEL, "hf_repo_id": "  "},
    ]
    codes = [
        (await client.post("/api/v1/models", json=case, headers=auth_headers(token))).status_code
        for case in cases
    ]

    assert codes == [422, 422, 422, 422]


async def test_delete_custom_model(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    created = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))
    model_id = created.json()["id"]

    deleted = await client.delete(f"/api/v1/models/{model_id}", headers=auth_headers(token))
    after = await client.get(f"/api/v1/models/{model_id}", headers=auth_headers(token))

    assert deleted.status_code == 204, deleted.text
    assert after.status_code == 404


async def test_delete_guards_builtin_and_deployed_models(
    client, db, deployment_factory
) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    builtin = (
        await db.execute(select(Model).where(Model.is_builtin.is_(True)).limit(1))
    ).scalar_one()
    custom = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))
    custom_id = uuid.UUID(custom.json()["id"])
    await deployment_factory(custom_id, cluster.id)

    reserved = await client.delete(f"/api/v1/models/{builtin.id}", headers=auth_headers(token))
    deployed = await client.delete(f"/api/v1/models/{custom_id}", headers=auth_headers(token))
    unknown = await client.delete(f"/api/v1/models/{uuid.uuid4()}", headers=auth_headers(token))

    assert reserved.status_code == 409
    assert reserved.json()["detail"] == "builtin models cannot be deleted"
    assert deployed.status_code == 409
    assert deployed.json()["detail"] == "model has deployments"
    assert unknown.status_code == 404


async def test_custom_model_deploys_and_respects_engine_gate(client, db) -> None:
    """A user-added model deploys like a builtin one, and the engine gate still applies."""
    from conftest import add_connected_worker

    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id)
    custom = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))
    audio = await client.post(
        "/api/v1/models",
        json={**NEW_MODEL, "hf_repo_id": "acme/whisper-large-v3", "category": "audio"},
        headers=auth_headers(token),
    )
    model = (await db.execute(select(Model).where(Model.id == uuid.UUID(custom.json()["id"])))).scalar_one()

    deploy = {
        "model_id": str(model.id),
        "cluster_id": str(cluster.id),
        "engine": "vllm",
        "profile": "latency",
        "replicas": 1,
        "gpu_selection": "auto",
    }
    unsupported = await client.post(
        "/api/v1/model-instances",
        json={**deploy, "model_id": audio.json()["id"]},
        headers=auth_headers(token),
    )
    accepted = await client.post("/api/v1/model-instances", json=deploy, headers=auth_headers(token))

    assert unsupported.status_code == 400
    assert "vllm" in unsupported.json()["detail"]
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert isinstance(body, list)
    assert [instance["state"] for instance in body] == ["scheduled"]
    assert body[0]["model"]["name"] == NEW_MODEL["hf_repo_id"]


async def test_seed_catalog_leaves_custom_models_alone(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    created = await client.post("/api/v1/models", json=NEW_MODEL, headers=auth_headers(token))

    await seed_catalog(db)

    model = (
        await db.execute(select(Model).where(Model.id == uuid.UUID(created.json()["id"])))
    ).scalar_one()
    assert model.is_builtin is False
    assert model.vram_required_mb == NEW_MODEL["vram_required_mb"]
    assert model.supported_engines == ["vllm", "sglang"]
