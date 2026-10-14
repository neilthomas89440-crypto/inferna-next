"""Usage metering: gateway accounting, aggregation, GPU occupancy, quotas, read endpoint."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from prometheus_client import REGISTRY
from sqlalchemy import select

from inferna_server.auth import hash_password
from inferna_server.main import app
from inferna_server.models import (
    ApiKey,
    Cluster,
    Deployment,
    GpuUsageRecord,
    Model,
    ModelInstance,
    UsageRecord,
    User,
)
from inferna_server.services import usage
from inferna_server.services.usage import (
    UsageContext,
    check_quota,
    flush_usage,
    record_request,
    sample_occupancy,
)

SSE_WITH_USAGE = (
    b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
    b'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    b"data: [DONE]\n\n"
)
SSE_WITHOUT_USAGE = (
    b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture(autouse=True)
def _clean_buffer():
    """The gateway buffer is process-global: never let one test bill another."""
    usage._PENDING.clear()
    yield
    usage._PENDING.clear()


@pytest.fixture
async def upstream(client):
    """Install MockTransport upstreams; every client created here is closed afterwards."""

    async def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    created: list[httpx.AsyncClient] = []

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(mock)
        app.state.gateway_client = mock

    install.explode = explode  # type: ignore[attr-defined]
    yield install
    app.state.gateway_client = None
    for mock in created:
        await mock.aclose()


def _sse(handler_body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=handler_body)

    return handler


async def _seed_instance(
    db, name: str, address: str | None = "127.0.0.1"
) -> tuple[Model, ModelInstance]:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = Model(
        name=name,
        display_name=name,
        category="llm",
        vram_required_mb=4096,
        requires_hf_token=False,
        supported_engines=["vllm"],
    )
    db.add(model)
    await db.flush()
    worker = await add_connected_worker(db, cluster.id, name=f"w-{name}")
    worker.address = address
    deployment = Deployment(
        model_id=model.id, cluster_id=cluster.id, engine="vllm", profile="latency"
    )
    db.add(deployment)
    await db.flush()
    instance = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        worker_id=worker.id,
        deployment=deployment,
        engine="vllm",
        profile="latency",
        gpu_indexes=[0],
        state="running",
        port=8010,
    )
    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    return model, instance


async def _admin_and_key(client) -> tuple[str, str, dict]:
    """(admin JWT, raw gateway key, key row)."""
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    created = await client.post("/api/v1/keys", json={"name": "usage"}, headers=auth_headers(token))
    assert created.status_code == 201, created.text
    listed = await client.get("/api/v1/keys", headers=auth_headers(token))
    return token, created.json()["key"], listed.json()[0]


async def _admin_user(db) -> User:
    return (await db.execute(select(User).where(User.username == TEST_ADMIN["username"]))).scalar_one()


async def _add_api_key(db, user: User, name: str = "usage-key") -> ApiKey:
    key = ApiKey(user_id=user.id, name=name, key_hash=f"h-{uuid.uuid4()}", scopes=[])
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key


async def _chat(client, key: str, model: str, body: dict | None = None) -> httpx.Response:
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    payload.update(body or {})
    return await client.post("/v1/chat/completions", headers=auth_headers(key), json=payload)


def _pending_total() -> tuple[int, int, int]:
    """(requests, tokens_in, tokens_out) currently buffered."""
    return (
        sum(accum.requests for accum in usage._PENDING.values()),
        sum(accum.tokens_in for accum in usage._PENDING.values()),
        sum(accum.tokens_out for accum in usage._PENDING.values()),
    )


def _counter(model: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "inferna_usage_events_total", {"model": model, "outcome": outcome}
        )
        or 0.0
    )


# --- gateway accounting ---


async def test_streaming_request_records_tokens(client, upstream, db) -> None:
    _, key, _ = await _admin_and_key(client)
    await _seed_instance(db, "usage-model")
    upstream(_sse(SSE_WITH_USAGE))

    resp = await _chat(client, key, "usage-model")

    assert resp.status_code == 200
    assert resp.content == SSE_WITH_USAGE
    assert _pending_total() == (1, 10, 5)
    assert _counter("usage-model", "ok") == 1.0


async def test_stream_without_usage_chunk_counts_the_request_only(client, upstream, db) -> None:
    _, key, _ = await _admin_and_key(client)
    await _seed_instance(db, "no-usage-model")
    upstream(_sse(SSE_WITHOUT_USAGE))

    resp = await _chat(client, key, "no-usage-model")

    assert resp.status_code == 200
    assert _pending_total() == (1, 0, 0)
    assert _counter("no-usage-model", "no_usage_chunk") == 1.0


async def test_client_disconnect_still_records_the_request(client, upstream, db) -> None:
    """Regression: accounting used to be skipped when the client hung up mid-stream."""
    _, key, _ = await _admin_and_key(client)
    await _seed_instance(db, "disconnect-usage-model")

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            await asyncio.sleep(0.5)
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            return None

    upstream(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SlowStream()
        )
    )

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={
            "model": "disconnect-usage-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        async for chunk in resp.aiter_bytes():
            assert chunk
            break
    await asyncio.sleep(0.1)

    requests, tokens_in, tokens_out = _pending_total()
    assert requests == 1
    assert (tokens_in, tokens_out) == (0, 0)


async def test_upstream_failure_records_the_request(client, upstream, db) -> None:
    _, key, _ = await _admin_and_key(client)
    await _seed_instance(db, "broken-upstream-model")
    upstream(upstream.explode)  # type: ignore[attr-defined]

    resp = await _chat(client, key, "broken-upstream-model")

    assert resp.status_code == 502
    assert _pending_total() == (1, 0, 0)
    assert _counter("broken-upstream-model", "upstream_error") == 1.0


async def test_reused_key_with_stored_stamp_still_serves(client, upstream, db) -> None:
    """Regression: a key whose stamp was persisted 500'd on every later request.

    `last_used_at` comes back naive from SQLite (tz-aware column, no tzinfo), so comparing it
    with `utcnow()` raised TypeError inside gateway auth — before any usage accounting.
    """
    from inferna_server.api.gateway import flush_last_used_stamps

    _, key, _ = await _admin_and_key(client)
    await _seed_instance(db, "stamp-model")
    upstream(_sse(SSE_WITH_USAGE))

    first = await _chat(client, key, "stamp-model")
    await flush_last_used_stamps(db)  # persist the stamp, as the background loop does
    db.expire_all()  # next read comes from the database (naive on SQLite)

    second = await _chat(client, key, "stamp-model")

    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert _pending_total() == (2, 20, 10)


async def test_stream_usage_is_requested_from_upstream(client, upstream, db) -> None:
    _, key, _ = await _admin_and_key(client)
    await _seed_instance(db, "inject-model")
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, content=SSE_WITH_USAGE)

    upstream(handler)

    await _chat(client, key, "inject-model")
    await _chat(client, key, "inject-model", {"stream_options": {"include_usage": False}})

    first = json.loads(bodies[0])
    second = json.loads(bodies[1])

    assert first["stream_options"] == {"include_usage": True}
    # A client's own stream_options is forwarded untouched.
    assert second["stream_options"] == {"include_usage": False}


# --- aggregation ---


async def test_flush_upserts_and_is_idempotent(db) -> None:
    model, _ = await _seed_instance(db, "flush-model")
    admin = await _admin_user(db)
    api_key = await _add_api_key(db, admin)
    ctx = UsageContext(
        user_id=admin.id,
        api_key_id=api_key.id,
        model_id=model.id,
        model_name="flush-model",
        instance_id=uuid.uuid4(),
        started=0.0,
    )

    record_request(ctx, tokens_in=3, tokens_out=4, latency_ms=100)
    record_request(ctx, tokens_in=1, tokens_out=1, latency_ms=50)
    assert await flush_usage(db) == 1

    row = (await db.execute(select(UsageRecord))).scalar_one()
    assert (row.requests, row.tokens_in, row.tokens_out, row.latency_ms_sum) == (2, 4, 5, 150)

    assert await flush_usage(db) == 0  # nothing buffered
    record_request(ctx, tokens_in=2, tokens_out=2, latency_ms=10)
    assert await flush_usage(db) == 1
    await db.refresh(row)
    assert (row.requests, row.tokens_in, row.tokens_out) == (3, 6, 7)


async def test_flush_requeues_on_failure() -> None:
    ctx = UsageContext(
        user_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        model_name="requeue-model",
        instance_id=uuid.uuid4(),
        started=0.0,
    )
    record_request(ctx, tokens_in=1, tokens_out=1)

    class BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("db down")

        async def commit(self) -> None:
            raise RuntimeError("db down")

        async def rollback(self) -> None:
            return None

    with pytest.raises(RuntimeError):
        await flush_usage(BrokenSession())  # type: ignore[arg-type]

    assert _pending_total() == (1, 1, 1)


async def test_occupancy_sampler_counts_gpu_seconds(db) -> None:
    model, _ = await _seed_instance(db, "occupancy-model")

    assert await sample_occupancy(db, 30.0) == 1
    row = (
        await db.execute(select(GpuUsageRecord).where(GpuUsageRecord.model_id == model.id))
    ).scalar_one()
    assert row.gpu_seconds == 30.0

    await sample_occupancy(db, 30.0)
    await db.refresh(row)
    assert row.gpu_seconds == 60.0


async def test_occupancy_ignores_terminal_instances(db) -> None:
    _, instance = await _seed_instance(db, "stopped-model")
    instance.state = "stopped"
    await db.commit()

    assert await sample_occupancy(db, 30.0) == 0


# --- quotas ---


async def test_quota_rejects_once_the_daily_limit_is_reached(client, upstream, db) -> None:
    _, key, key_row = await _admin_and_key(client)
    model, _ = await _seed_instance(db, "quota-model")
    upstream(_sse(SSE_WITH_USAGE))
    admin = await _admin_user(db)
    admin.request_limit = 1
    db.add(
        UsageRecord(
            user_id=admin.id,
            api_key_id=uuid.UUID(key_row["id"]),
            model_id=model.id,
            period_start=datetime.now(timezone.utc).date(),
            requests=1,
            tokens_in=0,
            tokens_out=0,
            latency_ms_sum=0,
        )
    )
    await db.commit()

    rejected = await _chat(client, key, "quota-model")

    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "quota_exceeded"
    assert _pending_total() == (0, 0, 0)
    assert _counter("quota-model", "quota_rejected") == 1.0


async def test_quota_allows_traffic_when_no_limit_is_set(db) -> None:
    admin = await _admin_user(db)

    assert await check_quota(db, admin) is None


async def test_set_limits_endpoint(client, db) -> None:
    admin_token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    alice = User(
        username="alice", password_hash=hash_password("secret"), role="user", is_active=True
    )
    db.add(alice)
    await db.commit()
    await db.refresh(alice)

    resp = await client.put(
        f"/api/v1/users/{alice.id}/limits",
        json={"token_limit": 1000, "request_limit": 50},
        headers=auth_headers(admin_token),
    )
    cleared = await client.put(
        f"/api/v1/users/{alice.id}/limits",
        json={"token_limit": None, "request_limit": None},
        headers=auth_headers(admin_token),
    )
    alice_token = await login(client, "alice", "secret")
    forbidden = await client.put(
        f"/api/v1/users/{alice.id}/limits",
        json={"token_limit": 1, "request_limit": None},
        headers=auth_headers(alice_token),
    )
    unknown = await client.put(
        f"/api/v1/users/{uuid.uuid4()}/limits",
        json={"token_limit": 1, "request_limit": None},
        headers=auth_headers(admin_token),
    )
    invalid = await client.put(
        f"/api/v1/users/{alice.id}/limits",
        json={"token_limit": -1, "request_limit": None},
        headers=auth_headers(admin_token),
    )

    assert resp.status_code == 200, resp.text
    assert (resp.json()["token_limit"], resp.json()["request_limit"]) == (1000, 50)
    assert cleared.json()["token_limit"] is None
    assert forbidden.status_code == 403
    assert unknown.status_code == 404
    assert invalid.status_code == 422


# --- read endpoint ---


async def _insert_usage(
    db, *, user, api_key, model, requests=2, tokens_in=5, tokens_out=7, days_ago=0
) -> None:
    day = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    db.add(
        UsageRecord(
            user_id=user.id,
            api_key_id=api_key.id,
            model_id=model.id,
            period_start=day,
            requests=requests,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms_sum=100 * requests,
        )
    )
    await db.commit()


async def test_usage_endpoint_scopes_rows_by_role(client, db) -> None:
    admin_token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    admin = await _admin_user(db)
    alice = User(
        username="alice", password_hash=hash_password("secret"), role="user", is_active=True
    )
    db.add(alice)
    await db.commit()
    await db.refresh(alice)
    admin_key = await _add_api_key(db, admin, "admin-key")
    alice_key = await _add_api_key(db, alice, "alice-key")
    model, _ = await _seed_instance(db, "report-model")
    await _insert_usage(db, user=admin, api_key=admin_key, model=model)
    await _insert_usage(db, user=alice, api_key=alice_key, model=model, requests=3, tokens_in=1, tokens_out=1)
    await _insert_usage(db, user=alice, api_key=alice_key, model=model, days_ago=30, requests=99)

    admin_view = await client.get("/api/v1/usage?days=7", headers=auth_headers(admin_token))
    alice_token = await login(client, "alice", "secret")
    alice_view = await client.get("/api/v1/usage?days=7", headers=auth_headers(alice_token))

    assert admin_view.status_code == 200, admin_view.text
    body = admin_view.json()
    assert {row["username"] for row in body["rows"]} == {"admin", "alice"}
    assert body["totals"]["requests"] == 5  # the 30-day-old row is outside the window
    assert body["totals"]["tokens_in"] == 6
    assert body["limits"]["token_limit"] is None

    assert alice_view.status_code == 200
    assert {row["username"] for row in alice_view.json()["rows"]} == {"alice"}
    assert alice_view.json()["totals"]["requests"] == 3


async def test_usage_endpoint_filters_and_bounds(client, db) -> None:
    admin_token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    admin = await _admin_user(db)
    alice = User(
        username="alice", password_hash=hash_password("secret"), role="user", is_active=True
    )
    db.add(alice)
    await db.commit()
    await db.refresh(alice)
    admin_key = await _add_api_key(db, admin, "admin-key")
    model, _ = await _seed_instance(db, "filter-model")
    await _insert_usage(db, user=admin, api_key=admin_key, model=model)

    by_model = await client.get(
        f"/api/v1/usage?model_id={model.id}", headers=auth_headers(admin_token)
    )
    alice_token = await login(client, "alice", "secret")
    foreign_key = await client.get(
        f"/api/v1/usage?key_id={admin_key.id}", headers=auth_headers(alice_token)
    )
    too_many_days = await client.get("/api/v1/usage?days=91", headers=auth_headers(admin_token))
    no_days = await client.get("/api/v1/usage?days=0", headers=auth_headers(admin_token))

    assert by_model.status_code == 200
    assert by_model.json()["totals"]["requests"] == 2
    assert foreign_key.status_code == 403
    assert (too_many_days.status_code, no_days.status_code) == (422, 422)
