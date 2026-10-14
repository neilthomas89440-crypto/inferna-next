"""Streaming client: TTFT/ITL extraction, usage parsing and failure handling."""

from __future__ import annotations

import json

import httpx

from bench.client import chat

CHUNK_1 = {"choices": [{"delta": {"content": "Hel"}}]}
CHUNK_2 = {"choices": [{"delta": {"content": "lo"}}]}
USAGE = {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


def _sse(*payloads: dict) -> bytes:
    body = b"".join(f"data: {json.dumps(payload)}\n\n".encode() for payload in payloads)
    return body + b"data: [DONE]\n\n"


async def _client(payload: bytes, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=payload, headers={"content-type": "text/event-stream"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_chat_measures_ttft_itl_and_usage() -> None:
    client = await _client(_sse(CHUNK_1, CHUNK_2, USAGE))
    async with client:
        result = await chat(
            client,
            "http://gateway.test",
            "key",
            "model",
            [{"role": "user", "content": "hi"}],
        )

    assert result.ok
    assert result.ttft_ms is not None and result.ttft_ms >= 0
    assert result.e2e_ms >= result.ttft_ms
    assert len(result.itl_ms) == 1  # one interval between two content chunks
    assert result.content_chunks == 2
    assert (result.prompt_tokens, result.completion_tokens) == (11, 7)
    assert result.generation_ms is not None


async def test_chat_reports_upstream_failure_without_raising() -> None:
    client = await _client(b'{"error":{"message":"model not found"}}', status=404)
    async with client:
        result = await chat(client, "http://gateway.test", "key", "nope", [])

    assert not result.ok
    assert result.status == 404
    assert "model not found" in (result.error or "")
    assert result.ttft_ms is None


async def test_chat_reports_transport_errors() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as client:
        result = await chat(client, "http://gateway.test", "key", "model", [])

    assert result.status == 0
    assert "ConnectError" in (result.error or "")
