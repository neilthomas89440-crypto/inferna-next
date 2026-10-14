"""End-to-end harness check: the streaming client against the deterministic fake upstream.

This is what makes the reproducibility gate meaningful: the upstream's pacing is fixed, so
two runs must produce the same TTFT distribution within a few percent.
"""

from __future__ import annotations

import httpx

from bench.client import chat
from bench.fake_upstream import FakeUpstream


async def test_client_parses_the_fake_upstream_stream() -> None:
    app = FakeUpstream(chunks=4, delay=0.0)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as client:
        result = await chat(
            client, "http://fake", "key", "fake-model", [{"role": "user", "content": "hi"}]
        )

    assert result.ok
    assert result.content_chunks == 4
    assert result.completion_tokens == 4
    assert result.prompt_tokens == 16
    assert result.ttft_ms is not None
    assert len(result.itl_ms) == 3


async def test_fake_upstream_requires_a_bearer_token() -> None:
    app = FakeUpstream(chunks=1, delay=0.0)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as client:
        unauthorized = await client.post(
            "/v1/chat/completions", json={"model": "fake-model", "messages": []}
        )
        missing = await client.post(
            "/v1/chat/completions",
            json={"model": "fake-model", "messages": []},
            headers={"Authorization": "Bearer key"},
        )
        other_path = await client.post("/v1/embeddings", json={})

    assert unauthorized.status_code == 401
    assert missing.status_code == 200
    assert other_path.status_code == 404
