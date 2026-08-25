"""Gateway routing tests: least-active selection, LRU tie-break, slot release."""

import asyncio

import httpx
import pytest
from conftest import TEST_ADMIN, auth_headers, login
from prometheus_client import REGISTRY

from inferna_server.api import gateway as gateway_api
from inferna_server.main import app

SSE_BODY = (
    b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
    b'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture(autouse=True)
def _reset_routing_state():
    """Gateway active/assignment counters are module-global; isolate each test."""
    gateway_api._active_by_instance.clear()
    gateway_api._last_assigned.clear()
    yield
    gateway_api._active_by_instance.clear()
    gateway_api._last_assigned.clear()


@pytest.fixture
async def gateway(client):
    """Route gateway upstream traffic into a MockTransport; capture every upstream URL."""
    captured: dict[str, object] = {"urls": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        captured["urls"].append(url)  # type: ignore[union-attr]
        captured["url"] = url
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=SSE_BODY
        )

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.gateway_client = mock
    yield captured
    app.state.gateway_client = None
    await mock.aclose()


async def _admin_and_key(client) -> str:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    resp = await client.post("/api/v1/keys", json={"name": "gw"}, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


async def _chat(client, key: str, model: str) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


async def test_least_loaded_instance_selected(client, gateway, db, seed_deployment) -> None:
    """A replica with an active request is skipped in favour of the idle one."""
    key = await _admin_and_key(client)
    seeded = await seed_deployment("least-loaded-model", replicas=2)
    older, newer = seeded["instances"]
    assert older.created_at < newer.created_at

    # The oldest instance already serves one request.
    gateway_api._active_by_instance[older.id] = 1

    resp = await _chat(client, key, "least-loaded-model")

    assert resp.status_code == 200
    # Traffic goes to the idle replica despite it being newer.
    assert gateway["urls"] == [f"http://127.0.0.1:{newer.port}/v1/chat/completions"]


async def test_idle_replicas_alternate_lru_tie_break(client, gateway, db, seed_deployment) -> None:
    """With zero active requests everywhere, consecutive requests rotate replicas."""
    key = await _admin_and_key(client)
    seeded = await seed_deployment("lru-model", replicas=2)
    older, newer = seeded["instances"]

    for _ in range(3):
        resp = await _chat(client, key, "lru-model")
        assert resp.status_code == 200

    expected = [
        f"http://127.0.0.1:{older.port}/v1/chat/completions",
        f"http://127.0.0.1:{newer.port}/v1/chat/completions",
        f"http://127.0.0.1:{older.port}/v1/chat/completions",
    ]
    # First hit goes to the oldest replica; LRU tie-break then rotates through the rest.
    assert gateway["urls"] == expected


async def test_active_slot_released_after_stream(client, gateway, db, seed_deployment) -> None:
    """After the streamed response completes, the active counter and gauge drain to 0."""
    key = await _admin_and_key(client)
    seeded = await seed_deployment("release-model", replicas=1)
    instance = seeded["instances"][0]

    resp = await _chat(client, key, "release-model")

    assert resp.status_code == 200
    assert resp.content == SSE_BODY
    assert gateway_api._active_by_instance.get(instance.id) is None
    gauge = REGISTRY.get_sample_value(
        "inferna_instance_active_requests",
        {"instance_id": str(instance.id), "model": "release-model"},
    )
    assert gauge == 0


async def test_busy_replica_regains_traffic_after_release(
    client, gateway, db, seed_deployment
) -> None:
    """Once its slots are released, previously busy replicas become routable again."""
    key = await _admin_and_key(client)
    seeded = await seed_deployment("regain-model", replicas=2)
    older, newer = seeded["instances"]

    gateway_api._active_by_instance[older.id] = 1
    first = await _chat(client, key, "regain-model")
    assert first.status_code == 200
    assert gateway["urls"][-1] == f"http://127.0.0.1:{newer.port}/v1/chat/completions"

    # The busy replica finished its own request; both are idle now and the oldest wins.
    gateway_api._active_by_instance.clear()
    second = await _chat(client, key, "regain-model")

    assert second.status_code == 200
    assert gateway["urls"][-1] == f"http://127.0.0.1:{older.port}/v1/chat/completions"


async def test_non_transport_upstream_error_releases_slot(
    client, gateway, db, seed_deployment
) -> None:
    """A crash inside the upstream handler (not a transport error) must not leak the slot."""
    key = await _admin_and_key(client)
    seeded = await seed_deployment("crash-model", replicas=1)
    instance = seeded["instances"][0]

    async def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("upstream crashed")

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.gateway_client = mock
    try:
        with pytest.raises(RuntimeError, match="upstream crashed"):
            await _chat(client, key, "crash-model")
    finally:
        app.state.gateway_client = None
        await mock.aclose()

    # The gauge drains back to 0, so the replica is routable again.
    assert gateway_api._active_by_instance.get(instance.id) is None
    gauge = REGISTRY.get_sample_value(
        "inferna_instance_active_requests",
        {"instance_id": str(instance.id), "model": "crash-model"},
    )
    assert gauge == 0


async def test_concurrent_requests_route_to_distinct_replicas(
    client, gateway, db, seed_deployment
) -> None:
    """Two simultaneous requests never share one replica's slot."""
    key = await _admin_and_key(client)
    seeded = await seed_deployment("concurrent-model", replicas=2)
    older, newer = seeded["instances"]

    first, second = await asyncio.gather(
        _chat(client, key, "concurrent-model"),
        _chat(client, key, "concurrent-model"),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(gateway["urls"]) == 2
    ports = {int(url.split(":", 3)[2].split("/", 1)[0]) for url in gateway["urls"]}
    # Least-loaded must not send the second request to the replica already
    # holding the first one's active slot.
    assert ports == {older.port, newer.port}
    for instance in (older, newer):
        assert gateway_api._active_by_instance.get(instance.id) is None
        gauge = REGISTRY.get_sample_value(
            "inferna_instance_active_requests",
            {"instance_id": str(instance.id), "model": "concurrent-model"},
        )
        assert gauge == 0


async def test_requests_continue_after_one_replica_stopped(
    client, gateway, db, seed_deployment
) -> None:
    """Stopping one replica must not break the model endpoint: traffic shifts to the survivor."""
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    key = await _admin_and_key(client)
    seeded = await seed_deployment("stop-resilience-model", replicas=2)
    older, newer = seeded["instances"]

    # Stop the newest replica via the real endpoint (desired_state="stopped",
    # generation bump), then apply the worker-reported observed state that
    # takes it out of LIVE_STATES — the gateway's routing filter.
    resp = await client.post(
        f"/api/v1/model-instances/{newer.id}/stop", headers=auth_headers(token)
    )
    assert resp.status_code == 200
    assert resp.json()["desired_state"] == "stopped"
    newer.state = "stopped"
    await db.commit()

    for _ in range(4):
        chat = await _chat(client, key, "stop-resilience-model")
        assert chat.status_code == 200

    # Every request reached only the surviving (oldest) replica.
    survivor_url = f"http://127.0.0.1:{older.port}/v1/chat/completions"
    assert gateway["urls"] == [survivor_url] * 4


async def test_slot_reserved_before_dns_resolve_window(
    client, gateway, db, seed_deployment, monkeypatch
) -> None:
    """The routing slot is reserved inside the selection lock, before the DNS
    resolve await. Two concurrent requests parked in the resolve window must
    observe each other's reservations (each replica exactly one active slot)
    and land on distinct replicas; afterwards both gauges drain to 0.
    """
    key = await _admin_and_key(client)
    seeded = await seed_deployment("reserve-window-model", replicas=2)
    older, newer = seeded["instances"]

    original_resolve = gateway_api.resolve_and_validate
    arrived = 0
    both_selected = asyncio.Event()
    release = asyncio.Event()

    async def gated_resolve(host, settings):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_selected.set()
        # Park both requests after their selection but before any upstream send.
        await release.wait()
        return await original_resolve(host, settings)

    monkeypatch.setattr(gateway_api, "resolve_and_validate", gated_resolve)

    tasks = [asyncio.create_task(_chat(client, key, "reserve-window-model")) for _ in range(2)]
    try:
        await asyncio.wait_for(both_selected.wait(), timeout=5)

        # Deterministic regression point: with selection and reservation split by
        # an await, these counters were still empty here and a third selection
        # (or a burst) would read stale zeros and pile onto one replica.
        assert sorted(gateway_api._active_by_instance.get(i.id, 0) for i in (older, newer)) == [
            1,
            1,
        ]

        release.set()
        first, second = await asyncio.gather(*tasks)
    finally:
        release.set()

    assert first.status_code == 200 and second.status_code == 200
    assert len(gateway["urls"]) == 2
    ports = {int(url.split(":", 3)[2].split("/", 1)[0]) for url in gateway["urls"]}
    assert ports == {older.port, newer.port}
    for instance in (older, newer):
        assert gateway_api._active_by_instance.get(instance.id) is None
        gauge = REGISTRY.get_sample_value(
            "inferna_instance_active_requests",
            {"instance_id": str(instance.id), "model": "reserve-window-model"},
        )
        assert gauge == 0
