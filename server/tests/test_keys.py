"""API key CRUD tests (creation, scoping, revoke, idempotency)."""

from __future__ import annotations

import re
import uuid

from conftest import TEST_ADMIN, auth_headers, login
from httpx import AsyncClient
from sqlalchemy import select

from inferna_server.models import ApiKey
from inferna_server.services.workers_svc import sha256_hex

KEY_PATTERN = re.compile(r"^inf-[0-9a-f]{32}$")


async def _admin_token(client: AsyncClient) -> str:
    return await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])


async def _create_key(client: AsyncClient, token: str, name: str = "test-key"):
    return await client.post("/api/v1/keys", json={"name": name}, headers=auth_headers(token))


async def _create_user(client: AsyncClient, token: str, username: str) -> str:
    resp = await client.post(
        "/api/v1/users",
        json={"username": username, "password": "secret1", "role": "user"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return await login(client, username, "secret1")


async def test_create_key_returns_plaintext_once(client: AsyncClient, db) -> None:
    token = await _admin_token(client)
    resp = await _create_key(client, token)
    assert resp.status_code == 201
    body = resp.json()
    assert KEY_PATTERN.match(body["key"])
    assert "key_hash" not in body

    # Stored hash matches the returned plaintext; list never leaks it.
    row = (
        await db.execute(select(ApiKey).where(ApiKey.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert row.key_hash == sha256_hex(body["key"])
    lst = await client.get("/api/v1/keys", headers=auth_headers(token))
    assert lst.status_code == 200
    listed = next(k for k in lst.json() if k["id"] == body["id"])
    assert "key" not in listed


async def test_non_admin_sees_only_own_keys(client: AsyncClient) -> None:
    token = await _admin_token(client)
    alice_token = await _create_user(client, token, "alice")

    admin_key = await _create_key(client, token, "admin-key")
    alice_key = await _create_key(client, alice_token, "alice-key")

    alice_lst = await client.get("/api/v1/keys", headers=auth_headers(alice_token))
    alice_ids = {k["id"] for k in alice_lst.json()}
    assert alice_key.json()["id"] in alice_ids
    assert admin_key.json()["id"] not in alice_ids

    admin_lst = await client.get("/api/v1/keys", headers=auth_headers(token))
    admin_ids = {k["id"] for k in admin_lst.json()}
    assert admin_key.json()["id"] in admin_ids
    assert alice_key.json()["id"] in admin_ids


async def test_revoke_owner_idempotent_foreign_forbidden_unknown_404(
    client: AsyncClient,
) -> None:
    token = await _admin_token(client)
    bob_token = await _create_user(client, token, "bob")
    carol_token = await _create_user(client, token, "carol")

    key = await _create_key(client, bob_token, "bob-key")
    key_id = key.json()["id"]

    # Owner revokes; second revoke is idempotent.
    r1 = await client.post(f"/api/v1/keys/{key_id}/revoke", headers=auth_headers(bob_token))
    assert r1.status_code == 200
    assert r1.json()["revoked_at"] is not None
    r2 = await client.post(f"/api/v1/keys/{key_id}/revoke", headers=auth_headers(bob_token))
    assert r2.status_code == 200

    # Non-owner, non-admin is forbidden.
    r3 = await client.post(f"/api/v1/keys/{key_id}/revoke", headers=auth_headers(carol_token))
    assert r3.status_code == 403

    # Unknown key id.
    r4 = await client.post(
        "/api/v1/keys/00000000-0000-0000-0000-000000000099/revoke",
        headers=auth_headers(token),
    )
    assert r4.status_code == 404
