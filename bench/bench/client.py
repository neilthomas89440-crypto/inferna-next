"""Streaming chat client: TTFT, inter-token latency, throughput, usage extraction."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx


@dataclass(slots=True)
class RequestResult:
    status: int
    e2e_ms: float
    ttft_ms: float | None = None
    itl_ms: list[float] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    content_chunks: int = 0
    error: str | None = None

    @property
    def generation_ms(self) -> float | None:
        """Time spent producing tokens (e2e minus time to first token)."""
        if self.ttft_ms is None:
            return None
        return self.e2e_ms - self.ttft_ms

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None


def _content_of(chunk: dict) -> str:
    """Text delta of a chat-completion chunk (streaming and non-streaming shapes)."""
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    message = choice.get("message") or {}
    message_content = message.get("content")
    return message_content if isinstance(message_content, str) else ""


async def chat(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model_name: str,
    messages: list[dict],
    *,
    stream: bool = True,
    max_tokens: int = 128,
    timeout: float = 180.0,
) -> RequestResult:
    """One chat completion; measures TTFT/ITL/TPS from the SSE stream.

    ``base_url`` is the origin (``http://host:port``); the path is always
    ``/v1/chat/completions`` so gateway and direct mode differ only in the origin.
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    started = time.perf_counter()
    first_content_at: float | None = None
    last_content_at: float | None = None
    itl: list[float] = []
    content_chunks = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=timeout
        ) as response:
            status = response.status_code
            if status != 200:
                body = (await response.aread()).decode("utf-8", "replace")[:400]
                return RequestResult(
                    status=status,
                    e2e_ms=(time.perf_counter() - started) * 1000,
                    error=f"HTTP {status}: {body}",
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage") or {}
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                text = _content_of(chunk)
                if not text:
                    continue
                now = time.perf_counter()
                if last_content_at is None:
                    first_content_at = now
                else:
                    itl.append((now - last_content_at) * 1000)
                last_content_at = now
                content_chunks += 1
    except httpx.HTTPError as exc:
        return RequestResult(
            status=0,
            e2e_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    e2e_ms = (time.perf_counter() - started) * 1000
    ttft_ms = (first_content_at - started) * 1000 if first_content_at is not None else None
    return RequestResult(
        status=200,
        e2e_ms=e2e_ms,
        ttft_ms=ttft_ms,
        itl_ms=itl,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        content_chunks=content_chunks,
    )
