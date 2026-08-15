"""Auth endpoint tests."""

from __future__ import annotations

from conftest import TEST_ADMIN, auth_headers, login
from httpx import AsyncClient

from inferna_server.auth import hash_password
from inferna_server.models import User


async def test_login_success(client: AsyncClient) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    assert token


async def test_login_wrong_password(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": TEST_ADMIN["username"], "password": "wrong"},
    )
    assert resp.status_code == 401


async def test_login_unknown_user(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


async def test_me_returns_profile(client: AsyncClient) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    resp = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == TEST_ADMIN["username"]
    assert body["role"] == "admin"


async def test_me_invalid_token(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me", headers=auth_headers("not-a-token"))
    assert resp.status_code == 401


async def test_login_inactive_user(client: AsyncClient, db) -> None:
    db.add(
        User(
            username="sleepy",
            password_hash=hash_password("secret1"),
            role="user",
            is_active=False,
        )
    )
    await db.commit()
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "sleepy", "password": "secret1"}
    )
    assert resp.status_code == 401


async def test_login_requires_json_body(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={})
    assert resp.status_code == 422


async def test_me_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
