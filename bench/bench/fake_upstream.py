"""Deterministic [OI]-compatible upstream used to measure the harness itself.

Serves ``/v1/chat/completions`` as SSE with a fixed number of content chunks and a final
usage block. It exists so the harness can be validated without a GPU or a real engine, and so
two runs of the same command are comparable (a reproducibility check needs a deterministic
server, not a live model).

Run it standalone:

    uv run --project bench uvicorn bench.fake_upstream:app --port 9100
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from typing import Any

DEFAULT_CHUNKS = 8
DEFAULT_DELAY_SECONDS = 0.02


class FakeUpstream:
    """Minimal ASGI app: streaming chat completions with configurable pacing."""

    def __init__(
        self,
        *,
        chunks: int = DEFAULT_CHUNKS,
        delay: float = DEFAULT_DELAY_SECONDS,
        model: str = "fake-model",
        require_auth: bool = True,
    ) -> None:
        self.chunks = chunks
        self.delay = delay
        self.model = model
        # Engines behind the gateway receive no client credentials (the gateway forwards only
        # `content-type` and `host`), so `require_auth=False` is how this fixture stands in for
        # a real engine; the default keeps it usable as a direct target in tests.
        self.require_auth = require_auth

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        if scope["method"] != "POST" or not path.endswith("/v1/chat/completions"):
            await self._plain(send, 404, b'{"error":{"message":"not found"}}')
            return
        if self.require_auth and not self._authorized(scope):
            await self._plain(send, 401, b'{"error":{"message":"unauthorized"}}')
            return

        body = await self._read_body(receive)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            await self._plain(send, 400, b'{"error":{"message":"invalid json"}}')
            return
        max_tokens = int(payload.get("max_tokens") or DEFAULT_CHUNKS)
        chunks = max(1, min(self.chunks, max_tokens))

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        for index in range(chunks):
            chunk = {
                "id": "fake-1",
                "object": "chat.completion.chunk",
                "model": self.model,
                "choices": [{"index": 0, "delta": {"content": f"token{index} "}}],
            }
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {json.dumps(chunk)}\n\n".encode(),
                    "more_body": True,
                }
            )
            await asyncio.sleep(self.delay)
        usage = {
            "id": "fake-1",
            "object": "chat.completion.chunk",
            "model": self.model,
            "choices": [],
            "usage": {"prompt_tokens": 16, "completion_tokens": chunks},
        }
        await send(
            {
                "type": "http.response.body",
                "body": f"data: {json.dumps(usage)}\n\n".encode(),
                "more_body": True,
            }
        )
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})

    @staticmethod
    def _authorized(scope: MutableMapping[str, Any]) -> bool:
        for key, value in scope.get("headers", []):
            if key == b"authorization" and value.startswith(b"Bearer "):
                return True
        return False

    @staticmethod
    async def _read_body(receive: Any) -> bytes:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                return body

    @staticmethod
    async def _plain(send: Any, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


app = FakeUpstream()


def main() -> int:
    """Serve the fake upstream (uvicorn is a dev dependency of the bench project)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="deterministic [OI]-compatible upstream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--chunks", type=int, default=DEFAULT_CHUNKS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="accept requests without a Bearer token (as an engine behind the gateway sees them)",
    )
    args = parser.parse_args()

    server_app = FakeUpstream(chunks=args.chunks, delay=args.delay, require_auth=not args.no_auth)
    print(f"fake upstream on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(server_app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
