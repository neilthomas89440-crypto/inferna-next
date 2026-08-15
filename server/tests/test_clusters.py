"""Cluster management tests."""

from __future__ import annotations

from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from httpx import AsyncClient
from sqlalchemy import select

from inferna_server.models import Cluster


async def _admin_token(client: AsyncClient) -> str:
    return await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])


async def test_list_clusters_includes_default(client: AsyncClient) -> None:
    token = await _admin_token(client)
    resp = await client.get("/api/v1/clusters", headers=auth_headers(token))
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert "default" in names


async def test_create_cluster_and_duplicate(client: AsyncClient) -> None:
    token = await _admin_token(client)
    resp = await client.post(
        "/api/v1/clusters",
        json={"name": "gpu-lab", "description": "Lab GPUs"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "gpu-lab"
    dup = await client.post(
        "/api/v1/clusters", json={"name": "gpu-lab"}, headers=auth_headers(token)
    )
    assert dup.status_code == 409


async def test_create_cluster_forbidden_for_user(client: AsyncClient) -> None:
    admin_token = await _admin_token(client)
    await client.post(
        "/api/v1/users",
        json={"username": "u1", "password": "secret1", "role": "user"},
        headers=auth_headers(admin_token),
    )
    user_token = await login(client, "u1", "secret1")
    resp = await client.post(
        "/api/v1/clusters", json={"name": "x"}, headers=auth_headers(user_token)
    )
    assert resp.status_code == 403


async def test_delete_cluster_with_workers_rejected(client: AsyncClient, db) -> None:
    token = await _admin_token(client)
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    await add_connected_worker(db, cluster.id, name="busy")
    resp = await client.delete(f"/api/v1/clusters/{cluster.id}", headers=auth_headers(token))
    assert resp.status_code == 400


async def test_delete_cluster(client: AsyncClient, db) -> None:
    import uuid

    token = await _admin_token(client)
    created = await client.post(
        "/api/v1/clusters", json={"name": "empty"}, headers=auth_headers(token)
    )
    cluster_id = created.json()["id"]
    resp = await client.delete(f"/api/v1/clusters/{cluster_id}", headers=auth_headers(token))
    assert resp.status_code == 204
    gone = await db.get(Cluster, uuid.UUID(cluster_id))
    assert gone is None
