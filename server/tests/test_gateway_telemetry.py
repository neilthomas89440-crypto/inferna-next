"""Serving telemetry: TTFT, inter-token latency and throughput observed at the gateway."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from prometheus_client import REGISTRY
from sqlalchemy import select

from inferna_server.main import app
from inferna_server.models import Cluster, Deployment, Model, ModelInstance

SSE_TWO_CHUNKS = (
    b'data: {"id":"c1","choices":[{"delta":{"role":"assistant"}}]}\n\n'
    b'data: {"id":"c1","choices":[{"delta":{"content":"Hel"}}]}\n\n'
    b'data: {"id":"c1","choices":[{"delta":{"content":"lo"}}]}\n\n'
    b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2}}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture
async def upstream(client):
    created: list[httpx.AsyncClient] = []

    def install(handler) -> None:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(mock)
        app.state.gateway_client = mock

    yield install
    app.state.gateway_client = None
    for mock in created:
        await mock.aclose()


async def _seed_instance(db, name: str, engine: str = "vllm") -> ModelInstance:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = Model(
        name=name,
        display_name=name,
        category="llm",
        vram_required_mb=4096,
        requires_hf_token=False,
        supported_engines=["vllm", "sglang"],
    )
    db.add(model)
    await db.flush()
    worker = await add_connected_worker(db, cluster.id, name=f"w-{name}")
    worker.address = "127.0.0.1"
    deployment = Deployment(
        model_id=model.id, cluster_id=cluster.id, engine=engine, profile="latency"
    )
    db.add(deployment)
    await db.flush()
    instance = ModelInstance(
        model_id=model.id,
        cluster_id=cluster.id,
        worker_id=worker.id,
        deployment=deployment,
        engine=engine,
        profile="latency",
        gpu_indexes=[0],
        state="running",
        port=8010,
    )
    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    return instance


async def _key(client) -> str:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    created = await client.post("/api/v1/keys", json={"name": "telemetry"}, headers=auth_headers(token))
    assert created.status_code == 201, created.text
    return created.json()["key"]


def _histogram_count(name: str, model: str, engine: str = "vllm", parallelism: str = "single") -> float:
    return (
        REGISTRY.get_sample_value(
            f"{name}_count", {"model": model, "engine": engine, "parallelism": parallelism}
        )
        or 0.0
    )


def _histogram_sum(name: str, model: str, engine: str = "vllm", parallelism: str = "single") -> float:
    return (
        REGISTRY.get_sample_value(
            f"{name}_sum", {"model": model, "engine": engine, "parallelism": parallelism}
        )
        or 0.0
    )


async def test_streaming_observes_ttft_itl_and_throughput(client, upstream, db) -> None:
    model_name = "telemetry-model"
    await _seed_instance(db, model_name)
    key = await _key(client)
    upstream(lambda request: httpx.Response(200, content=SSE_TWO_CHUNKS))

    before = (
        _histogram_count("inferna_ttft_seconds", model_name),
        _histogram_count("inferna_inter_token_seconds", model_name),
        _histogram_count("inferna_output_tps", model_name),
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": model_name, "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200

    ttft_count, itl_count, tps_count = (
        _histogram_count("inferna_ttft_seconds", model_name),
        _histogram_count("inferna_inter_token_seconds", model_name),
        _histogram_count("inferna_output_tps", model_name),
    )

    # One observation each: the role-only chunk must not count as first content.
    assert ttft_count == before[0] + 1
    assert itl_count == before[1] + 1  # two content chunks -> one interval
    assert tps_count == before[2] + 1
    assert _histogram_sum("inferna_ttft_seconds", model_name) > 0
    assert _histogram_sum("inferna_output_tps", model_name) > 0


async def test_engine_label_follows_the_instance(client, upstream, db) -> None:
    model_name = "sglang-model"
    await _seed_instance(db, model_name, engine="sglang")
    key = await _key(client)
    upstream(lambda request: httpx.Response(200, content=SSE_TWO_CHUNKS))

    await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": model_name, "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert _histogram_count("inferna_ttft_seconds", model_name, engine="sglang") == 1
    assert _histogram_count("inferna_ttft_seconds", model_name, engine="vllm") == 0


async def test_no_telemetry_when_the_upstream_never_streams_content(client, upstream, db) -> None:
    model_name = "empty-stream-model"
    await _seed_instance(db, model_name)
    key = await _key(client)
    upstream(lambda request: httpx.Response(200, content=b"data: [DONE]\n\n"))

    await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": model_name, "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert _histogram_count("inferna_ttft_seconds", model_name) == 0


async def test_disconnect_still_publishes_ttft(client, upstream, db) -> None:
    """The client hanging up mid-stream must not lose the measurement it already earned."""
    model_name = "disconnect-telemetry-model"
    await _seed_instance(db, model_name)
    key = await _key(client)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            await asyncio.sleep(0.4)
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
        json={"model": model_name, "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as response:
        async for chunk in response.aiter_bytes():
            assert chunk
            break
    await asyncio.sleep(0.1)

    assert _histogram_count("inferna_ttft_seconds", model_name) == 1


def test_content_delta_parser_ignores_non_content_chunks() -> None:
    from inferna_server.api.gateway import _content_deltas

    role_only = json.dumps({"choices": [{"delta": {"role": "assistant"}}]})
    empty = json.dumps({"choices": [{"delta": {"content": ""}}]})
    content = json.dumps({"choices": [{"delta": {"content": "Hi"}}]})
    done = "[DONE]"

    text = "\n".join(
        f"data: {payload}" for payload in (role_only, empty, content, done, "not json")
    )

    assert list(_content_deltas(text)) == ["Hi"]
    assert list(_content_deltas("")) == []
