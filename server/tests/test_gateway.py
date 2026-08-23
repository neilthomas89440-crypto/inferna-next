"""Inference gateway tests: auth, model resolution, SSE pass-through, metrics."""

from __future__ import annotations

import httpx
import pytest
from conftest import TEST_ADMIN, auth_headers, login
from prometheus_client import REGISTRY
from sqlalchemy import select

from inferna_server.main import app
from inferna_server.models import Cluster, Model, ModelInstance, Worker
from inferna_server.services.workers_svc import sha256_hex

SSE_BODY = (
    b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
    b'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture
async def gateway(client, db):
    """Route gateway upstream traffic into a MockTransport; capture the request."""
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=SSE_BODY
        )

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.gateway_client = mock
    yield captured
    app.state.gateway_client = None
    await mock.aclose()


async def _seed_instance(
    db, name: str, display_name: str, port: int = 8010, address: str = "127.0.0.1"
) -> Model:
    """Create a model with one connected worker + one running instance."""
    cluster = (
        await db.execute(select(Cluster).where(Cluster.name == "default"))
    ).scalar_one()
    model = Model(
        name=name,
        display_name=display_name,
        category="llm",
        vram_required_mb=4096,
        requires_hf_token=False,
        supported_engines=["vllm"],
    )
    db.add(model)
    await db.flush()
    worker = Worker(
        cluster_id=cluster.id,
        name=f"w-{name}",
        hostname="unresolvable-host",
        state="connected",
        token_hash=sha256_hex("tok"),
        address=address,
    )
    db.add(worker)
    await db.flush()
    db.add(
        ModelInstance(
            model_id=model.id,
            cluster_id=cluster.id,
            worker_id=worker.id,
            engine="vllm",
            profile="latency",
            gpu_indexes=[0],
            state="running",
            port=port,
        )
    )
    await db.commit()
    return model


async def _api_key(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post(
        "/api/v1/keys", json={"name": "gw"}, headers=auth_headers(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


async def _admin_and_key(client: httpx.AsyncClient) -> tuple[str, str]:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    return token, await _api_key(client, token)


# --- auth ---


async def test_missing_credentials_401(client, gateway) -> None:
    resp = await client.post("/v1/chat/completions", json={"model": "x"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"
    assert resp.headers.get("www-authenticate") == "Bearer"


async def test_garbage_key_401(client, gateway) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers("inf-not-a-real-key"),
        json={"model": "x"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_revoked_key_401(client, gateway) -> None:
    token, key = await _admin_and_key(client)
    keys = (await client.get("/api/v1/keys", headers=auth_headers(token))).json()
    key_id = next(k["id"] for k in keys if k["name"] == "gw")
    rv = await client.post(f"/api/v1/keys/{key_id}/revoke", headers=auth_headers(token))
    assert rv.status_code == 200
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers(key), json={"model": "x"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


# --- resolution ---


async def test_unknown_model_404(client, gateway) -> None:
    _, key = await _admin_and_key(client)
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers(key), json={"model": "no-such-model"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_model_without_live_instance_404(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    db.add(
        Model(
            name="no-instance-model",
            display_name="No Instance",
            category="llm",
            vram_required_mb=4096,
            requires_hf_token=False,
            supported_engines=["vllm"],
        )
    )
    await db.commit()
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers(key), json={"model": "no-instance-model"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


# --- proxy ---


async def test_streaming_passthrough(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "proxy-model", "Proxy Model")
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": "proxy-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == SSE_BODY
    assert gateway["url"] == "http://127.0.0.1:8010/v1/chat/completions"


async def test_non_stream_passthrough(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "proxy-model-2", "Proxy Model 2")
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": "proxy-model-2", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert resp.status_code == 200
    assert resp.content == SSE_BODY
    assert gateway["url"] == "http://127.0.0.1:8010/v1/chat/completions"


async def test_list_models(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "aaa-model", "Aaa Model")
    resp = await client.get("/v1/models", headers=auth_headers(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "aaa-model"
    assert body["data"][0]["object"] == "model"
    assert body["data"][0]["owned_by"] == "inferna"
    assert isinstance(body["data"][0]["created"], int)


# --- model extraction ---


async def test_octet_stream_missing_model_400(client, gateway) -> None:
    _, key = await _admin_and_key(client)
    resp = await client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}", "content-type": "application/octet-stream"},
        content=b"fake-audio-bytes",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_model"


async def test_octet_stream_model_from_query_proxied(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "audio-model", "Audio Model")
    resp = await client.post(
        "/v1/audio/transcriptions?model=audio-model",
        headers=auth_headers(key),
        content=b"fake-audio-bytes",
    )
    assert resp.status_code == 200
    assert gateway["url"] == "http://127.0.0.1:8010/v1/audio/transcriptions?model=audio-model"


async def test_non_object_json_body_with_query_model_proxied(client, gateway, db) -> None:
    """Valid JSON that is not an object must not crash the proxy (greptile P1)."""
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "json-arr-model", "Json Arr Model")
    resp = await client.post(
        "/v1/chat/completions?model=json-arr-model",
        headers=auth_headers(key),
        json=[1, 2, 3],
    )
    assert resp.status_code == 200
    assert resp.content == SSE_BODY
    assert gateway["url"] == "http://127.0.0.1:8010/v1/chat/completions?model=json-arr-model"


async def test_malformed_worker_address_502(client, gateway, db) -> None:
    """A worker with an unparsable registered address yields upstream_error, not a 500."""
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "bad-addr-model", "Bad Addr Model", address="127.0.0.1:notaport")
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": "bad-addr-model", "stream": True},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"


async def test_multipart_model_extraction(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "mp-model", "Mp Model")
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        "mp-model\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
        "fake-wav-bytes\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    resp = await client.post(
        "/v1/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {key}",
            "content-type": f"multipart/form-data; boundary={boundary}",
        },
        content=body,
    )
    assert resp.status_code == 200
    assert gateway["url"] == "http://127.0.0.1:8010/v1/audio/transcriptions"


# --- metrics ---


async def test_metrics_recorded(client, gateway, db) -> None:
    _, key = await _admin_and_key(client)
    await _seed_instance(db, "metric-model", "Metric Model")
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": "metric-model", "stream": True},
    )
    assert resp.status_code == 200
    assert (
        REGISTRY.get_sample_value(
            "inferna_requests_total", {"model": "metric-model", "status": "200"}
        )
        == 1
    )
    assert (
        REGISTRY.get_sample_value(
            "inferna_request_duration_seconds_count", {"model": "metric-model"}
        )
        == 1
    )
    assert (
        REGISTRY.get_sample_value(
            "inferna_tokens_total", {"model": "metric-model", "kind": "prompt"}
        )
        == 10
    )
    assert (
        REGISTRY.get_sample_value(
            "inferna_tokens_total", {"model": "metric-model", "kind": "completion"}
        )
        == 5
    )
