"""User management tests (RBAC)."""

from __future__ import annotations

from conftest import TEST_ADMIN, auth_headers, login
from httpx import AsyncClient
from sqlalchemy import select

from inferna_server.models import User


async def _admin_token(client: AsyncClient) -> str:
    return await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])


async def test_list_users(client: AsyncClient) -> None:
    token = await _admin_token(client)
    resp = await client.get("/api/v1/users", headers=auth_headers(token))
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()]
    assert TEST_ADMIN["username"] in usernames


async def test_create_user_and_duplicate(client: AsyncClient) -> None:
    token = await _admin_token(client)
    resp = await client.post(
        "/api/v1/users",
        json={"username": "alice", "password": "secret1", "role": "user"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "user"
    dup = await client.post(
        "/api/v1/users",
        json={"username": "alice", "password": "secret1", "role": "user"},
        headers=auth_headers(token),
    )
    assert dup.status_code == 409


async def test_create_user_forbidden_for_user_role(client: AsyncClient) -> None:
    admin_token = await _admin_token(client)
    await client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "secret1", "role": "user"},
        headers=auth_headers(admin_token),
    )
    bob_token = await login(client, "bob", "secret1")
    resp = await client.post(
        "/api/v1/users",
        json={"username": "eve", "password": "secret1", "role": "user"},
        headers=auth_headers(bob_token),
    )
    assert resp.status_code == 403


async def test_delete_self_rejected(client: AsyncClient) -> None:
    token = await _admin_token(client)
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    resp = await client.delete(f"/api/v1/users/{me['id']}", headers=auth_headers(token))
    assert resp.status_code == 400


async def test_delete_last_admin_rejected(client: AsyncClient, db) -> None:
    # Second admin B; deactivate the seeded admin; B must not delete A (last active admin).
    await client.post(
        "/api/v1/users",
        json={"username": "badmin", "password": "secret1", "role": "admin"},
        headers=auth_headers(await _admin_token(client)),
    )
    seeded = (
        await db.execute(select(User).where(User.username == TEST_ADMIN["username"]))
    ).scalar_one()
    seeded.is_active = False
    await db.commit()
    b_token = await login(client, "badmin", "secret1")
    resp = await client.delete(f"/api/v1/users/{seeded.id}", headers=auth_headers(b_token))
    assert resp.status_code == 400


async def test_delete_user(client: AsyncClient) -> None:
    token = await _admin_token(client)
    created = await client.post(
        "/api/v1/users",
        json={"username": "carol", "password": "secret1", "role": "user"},
        headers=auth_headers(token),
    )
    user_id = created.json()["id"]
    resp = await client.delete(f"/api/v1/users/{user_id}", headers=auth_headers(token))
    assert resp.status_code == 204


async def test_change_password_self(client: AsyncClient) -> None:
    token = await _admin_token(client)
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    resp = await client.put(
        f"/api/v1/users/{me['id']}/password",
        json={"password": "newpass1"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    # Old password no longer works, new one does.
    old = await client.post(
        "/api/v1/auth/login",
        json={"username": TEST_ADMIN["username"], "password": TEST_ADMIN["password"]},
    )
    assert old.status_code == 401
    new = await client.post(
        "/api/v1/auth/login",
        json={"username": TEST_ADMIN["username"], "password": "newpass1"},
    )
    assert new.status_code == 200


async def test_change_password_other_forbidden(client: AsyncClient) -> None:
    admin_token = await _admin_token(client)
    await client.post(
        "/api/v1/users",
        json={"username": "mallory", "password": "secret1", "role": "user"},
        headers=auth_headers(admin_token),
    )
    mallory_token = await login(client, "mallory", "secret1")
    admin = (await client.get("/api/v1/auth/me", headers=auth_headers(admin_token))).json()
    resp = await client.put(
        f"/api/v1/users/{admin['id']}/password",
        json={"password": "hacked1"},
        headers=auth_headers(mallory_token),
    )
    assert resp.status_code == 403
