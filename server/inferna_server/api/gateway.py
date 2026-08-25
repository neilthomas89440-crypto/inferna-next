"""OpenAI-compatible inference gateway: API-key auth, upstream resolution, SSE pass-through."""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.background import BackgroundTask

from inferna_server.config import get_settings
from inferna_server.db import get_db
from inferna_server.models import (
    LIVE_STATES,
    ApiKey,
    Model,
    ModelInstance,
    utcnow,
)
from inferna_server.services.metrics import (
    inferna_instance_active_requests,
    inferna_requests,
    inferna_time_to_first_byte_seconds,
    inferna_tokens,
)
from inferna_server.services.upstream_guard import (
    _extract_host,
    hostname_in_allowlist,
    resolve_and_validate,
)
from inferna_server.services.workers_svc import sha256_hex

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["gateway"])

_USAGE_RE = re.compile(r'"usage"\s*:\s*\{[^}]*\}')
_PROMPT_TOKENS_RE = re.compile(r'"prompt_tokens"\s*:\s*(\d+)')
_COMPLETION_TOKENS_RE = re.compile(r'"completion_tokens"\s*:\s*(\d+)')
# How often a key's last_used_at is stamped; avoids a DB write on every request.
LAST_USED_UPDATE_SECONDS = 60
# nginx caps the whole gateway at 100 MiB; enforce the same ceiling here so the
# :8000 port (reachable directly, bypassing nginx) cannot be abused with an
# unbounded request body. Audio transcription's contract is 100 MiB.
MAX_BODY_SIZE = 100 * 1024 * 1024
# JSON/text payloads (chat, embeddings) are far smaller in practice; cap tighter.
MAX_JSON_BODY_SIZE = 10 * 1024 * 1024
# Streaming responses have no total deadline, but a per-read *idle* timeout
# reaps a stalled upstream; 3600s matches the nginx proxy_read_timeout so a
# legitimately slow SSE stream is never cut short by either layer.
STREAM_IDLE_TIMEOUT_SECONDS = 3600.0

# Inferna API key format: inf- + 32 hex chars
_gateway_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="inf-<32 hex>",
    description="Inferna API key — 'inf-' followed by 32 hex characters, created in the web UI.",
)

# last_used_at stamps are collected in memory and persisted by a background
# task in main.py, so the request-scoped session never COMMITs on the hot path.

_last_used_dirty: dict[uuid.UUID, datetime] = {}
_last_used_lock = asyncio.Lock()

# Least-loaded routing state, in-memory like _last_used_dirty (single-node
# server per PRODUCT_SPEC; a second node would need shared storage).
_active_by_instance: dict[uuid.UUID, int] = {}
_last_assigned: dict[uuid.UUID, int] = {}
_assign_seq = itertools.count(1)
_active_lock = asyncio.Lock()


class OpenAIError(Exception):
    """Carries an OpenAI-style error; the app-level handler renders it as JSON."""

    def __init__(
        self,
        status_code: int,
        message: str,
        type_: str,
        code: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type_ = type_
        self.code = code
        self.headers = headers


def openai_error(
    status_code: int,
    message: str,
    type_: str,
    code: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": type_, "code": code}},
        headers=headers,
    )


async def openai_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, OpenAIError):
        raise exc
    # 502s are counted in _proxy with the real model label; count the rest here
    # (model is not reliably parseable for auth/model-resolution failures).
    if exc.status_code != 502 and request.url.path.startswith("/v1"):
        inferna_requests.labels(model="unknown", status=str(exc.status_code)).inc()
    return openai_error(exc.status_code, exc.message, exc.type_, exc.code, headers=exc.headers)


async def get_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_gateway_bearer),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    """Gateway auth: `Authorization: Bearer inf-<key>`; rejects missing/unknown/revoked/inactive."""

    def unauthorized() -> OpenAIError:
        return OpenAIError(
            401,
            "Invalid API key",
            "invalid_request_error",
            "invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None:
        raise unauthorized()
    key = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.key_hash == sha256_hex(credentials.credentials))
            .options(selectinload(ApiKey.user))
        )
    ).scalar_one_or_none()
    if key is None or key.revoked_at is not None or key.user is None or not key.user.is_active:
        raise unauthorized()
    if "inference" not in key.scopes:
        raise OpenAIError(
            403, "API key lacks inference scope", "invalid_request_error", "insufficient_scope"
        )
    # Best-effort usage stamp: at most once per minute, never on the per-request hot path.
    if key.last_used_at is None or (
        utcnow() - key.last_used_at
    ).total_seconds() >= LAST_USED_UPDATE_SECONDS:
        now = utcnow()
        key.last_used_at = now
        async with _last_used_lock:
            _last_used_dirty[key.id] = now
    return key


async def flush_last_used_stamps(db: AsyncSession) -> int:
    """Persist pending last_used_at stamps with a single commit; returns count.

    The dirty dict is snapshotted under the lock so the DB round-trip never
    blocks get_api_key stamp writes; on failure the snapshot is re-queued
    (newer stamps win via setdefault) for the next cycle.
    """
    async with _last_used_lock:
        if not _last_used_dirty:
            return 0
        pending = dict(_last_used_dirty)
        _last_used_dirty.clear()
    try:
        for key_id, stamp in pending.items():
            await db.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=stamp)
            )
        await db.commit()
    except Exception:
        async with _last_used_lock:
            for key_id, stamp in pending.items():
                _last_used_dirty.setdefault(key_id, stamp)
        raise
    return len(pending)


def _extract_multipart_model(content_type: str, raw_body: bytes) -> str | None:
    """Parse-only extraction of the `model` form field; raw bytes are forwarded upstream."""
    _, params = parse_options_header(
        content_type.encode("latin-1") if isinstance(content_type, str) else content_type
    )
    boundary = params.get(b"boundary")
    if not boundary:
        return None

    model: str | None = None
    field_name: str | None = None
    in_headers = True
    header_field = bytearray()
    header_value = bytearray()
    part_data = bytearray()

    def _noop(*_args: object) -> None:
        pass

    def _on_part_begin() -> None:
        nonlocal field_name, in_headers
        field_name = None
        in_headers = True

    def _on_header_field(data: bytes, start: int, end: int) -> None:
        nonlocal header_field
        if in_headers:
            header_field += data[start:end]

    def _on_header_value(data: bytes, start: int, end: int) -> None:
        nonlocal header_value
        if in_headers:
            header_value += data[start:end]

    def _on_header_end() -> None:
        nonlocal field_name, header_field, header_value
        if not in_headers:
            return
        header = header_field.decode("utf-8", "replace").strip().lower()
        value = header_value.decode("utf-8", "replace")
        if header == "content-disposition":
            match = re.search(r'name="([^"]*)"', value)
            if match is not None:
                field_name = match.group(1)
        header_field.clear()
        header_value.clear()

    def _on_headers_finished() -> None:
        nonlocal in_headers
        in_headers = False

    def _on_part_data(data: bytes, start: int, end: int) -> None:
        nonlocal part_data
        if field_name == "model":
            part_data += data[start:end]

    def _on_part_end() -> None:
        nonlocal model, part_data
        if field_name == "model":
            model = part_data.decode("utf-8", "replace")
        part_data.clear()

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": _on_part_begin,
            "on_part_data": _on_part_data,
            "on_part_end": _on_part_end,
            "on_header_field": _on_header_field,
            "on_header_value": _on_header_value,
            "on_header_end": _on_header_end,
            "on_headers_finished": _on_headers_finished,
            "on_end": _noop,
        },
    )
    try:
        parser.write(raw_body)
    except Exception as exc:
        # Malformed multipart (bad framing vs the declared boundary; python-
        # multipart raises ValueError/MultipartError variants) is a client
        # error like invalid JSON — 400, not an unhandled 500.
        raise OpenAIError(
            400,
            "invalid multipart body",
            "invalid_request_error",
            "invalid_request_error",
        ) from exc
    return model


# non-JSON, non-multipart bodies (e.g. raw audio octet-stream) have no model
# field — require ?model= query param
async def _extract_model(request: Request, raw_body: bytes) -> str:
    content_type = request.headers.get("content-type", "")
    lowered = content_type.lower()
    model: str | None = None
    if lowered.startswith("application/json"):
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and "model" in payload:
            if not isinstance(payload["model"], str):
                raise OpenAIError(
                    400, "missing 'model' field", "invalid_request_error", "missing_model"
                )
            model = payload["model"]
    elif lowered.startswith("multipart/form-data"):
        model = await asyncio.to_thread(_extract_multipart_model, content_type, raw_body)
    if model is None:
        model = request.query_params.get("model")
    if not model:
        raise OpenAIError(
            400, "missing 'model' field", "invalid_request_error", "missing_model"
        )
    return model


async def _read_body_limited(request: Request, limit: int = MAX_BODY_SIZE) -> bytes:
    """Read the request body, rejecting oversized payloads with a 413.

    Enforces ``Content-Length`` up front (no need to stream a known-too-large
    body) and caps streaming reads at *limit* so a malicious client cannot
    exhaust memory on the directly-reachable :8000 port. Works for JSON and
    multipart/raw bodies alike; the returned bytes are forwarded upstream.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > limit:
            raise OpenAIError(
                413,
                "request body too large",
                "invalid_request_error",
                "body_too_large",
            )
    body: bytearray = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise OpenAIError(
                413,
                "request body too large",
                "invalid_request_error",
                "body_too_large",
            )
    return bytes(body)

async def _resolve_target(
    db: AsyncSession, model_name: str, request: Request
) -> tuple[str, ModelInstance, Model]:
    model = (
        await db.execute(select(Model).where(Model.name == model_name))
    ).scalar_one_or_none()
    if model is None:
        raise OpenAIError(
            404, f"model '{model_name}' not found", "invalid_request_error", "model_not_found"
        )
    # Least-loaded: pick the live instance with the fewest active requests;
    # ties broken by least-recently-assigned (LRU), so a fresh replica (no
    # entry -> key 0) warms up first without synthetic traffic. Selection and
    # the LRU stamp below share one _active_lock critical section: two
    # concurrent requests must never observe the same (0, 0) snapshot and
    # both land on the same idle replica. No awaits inside the lock.
    rows = (
        (
            await db.execute(
                select(ModelInstance)
                .where(
                    ModelInstance.model_id == model.id,
                    ModelInstance.state.in_(LIVE_STATES),
                    ModelInstance.port.isnot(None),
                )
                .options(selectinload(ModelInstance.worker))
                .order_by(ModelInstance.created_at)
            )
        )
        .scalars()
        .all()
    )
    async with _active_lock:
        instance = (
            min(
                rows,
                key=lambda i: (_active_by_instance.get(i.id, 0), _last_assigned.get(i.id, 0)),
            )
            if rows
            else None
        )
        if instance is not None:
            _last_assigned[instance.id] = next(_assign_seq)
    if instance is None or instance.worker is None:
        raise OpenAIError(
            404,
            f"model '{model_name}' has no running instance",
            "invalid_request_error",
            "model_not_found",
        )
    worker = instance.worker
    assert worker is not None  # guaranteed by the guard above
    raw_host = worker.address or worker.hostname
    settings = get_settings()
    try:
        # Pin the validated IP (or original hostname) so the upstream connection
        # cannot re-resolve DNS at connect time — the SSRF hole where a host
        # resolves to an allowed IP at check time but a blocked IP at connect.
        validated = await resolve_and_validate(raw_host, settings)
    except ValueError as exc:
        logger.warning(
            "gateway upstream target blocked",
            worker=instance.worker.name,
            host=raw_host,
            reason=str(exc),
        )
        # SSRF-blocked 502s are raised here (before _proxy's counter), so record
        # them; the app-level handler skips 502s to avoid double counting.
        inferna_requests.labels(model=model_name, status="502").inc()
        raise OpenAIError(
            502, "upstream target not allowed", "api_error", "upstream_not_allowed"
        ) from exc
    # Build target from the pinned address (IP literal or original hostname).
    scheme = raw_host.split("://", 1)[0].lower() if "://" in raw_host else "http"
    if scheme == "https" and validated != _extract_host(raw_host):
        # Connecting to https://<ip> breaks TLS SNI/hostname verification, so the
        # pinned IP cannot be used. Passing the hostname through would re-resolve
        # DNS at connect time — the rebinding TOCTOU — so that is only acceptable
        # when the hostname itself is the trust anchor: development mode, or an
        # exact allowlist entry. Anything else is rejected rather than proxied.
        fallback_host = _extract_host(raw_host)
        trusted = (
            settings.environment == "development"
            or hostname_in_allowlist(fallback_host, settings)
        )
        if not trusted:
            logger.warning(
                "gateway https upstream rejected (no pin, no trust anchor)",
                worker=instance.worker.name,
                host=fallback_host,
            )
            raise OpenAIError(
                502,
                "https upstream requires an exact allowlist entry",
                "api_error",
                "upstream_not_allowed",
            )
        # _extract_host strips brackets from IPv6 literals; re-add them or the
        # target authority becomes invalid (https://::1:8010/...).
        try:
            ipaddress.ip_address(fallback_host)
            is_v6 = ":" in fallback_host
        except ValueError:
            is_v6 = False
        if is_v6:
            fallback_host = f"[{fallback_host}]"
        logger.warning(
            "gateway https upstream not IP-pinned (SNI)",
            worker=instance.worker.name,
            host=fallback_host,
        )
        validated = fallback_host
    # Workers must serve the OpenAI-compatible paths under /v1 (see
    # docs/architecture.md); the gateway prefix is forwarded verbatim.
    target = f"{scheme}://{validated}:{instance.port}{request.url.path}"
    if request.url.query:
        target += f"?{request.url.query}"
    return target, instance, model


def get_gateway_client(app: FastAPI) -> httpx.AsyncClient:
    """Lifespan-injected client; tests override it via app.state.gateway_client."""
    client = getattr(app.state, "gateway_client", None)
    if client is None:
        raise RuntimeError("gateway client not configured (app.state.gateway_client)")
    return client


def _record_usage(model_name: str, tail: list[str]) -> None:
    """Best-effort usage extraction from the tail of a streamed chat completion."""
    text = "".join(tail)
    match = _USAGE_RE.search(text)
    if match is None:
        return
    prompt = _PROMPT_TOKENS_RE.search(match.group(0))
    completion = _COMPLETION_TOKENS_RE.search(match.group(0))
    if prompt is not None:
        inferna_tokens.labels(model_name, "prompt").inc(int(prompt.group(1)))
    if completion is not None:
        inferna_tokens.labels(model_name, "completion").inc(int(completion.group(1)))


async def _relay(resp: httpx.Response, model_name: str, path: str) -> AsyncIterator[bytes]:
    # Tail buffer for usage extraction (chat + embeddings); bytes always yielded first.
    # Audio transcription responses carry no usage from OpenAI upstreams.
    tail: list[str] = []
    try:
        async for chunk in resp.aiter_bytes():
            if path in ("/v1/chat/completions", "/v1/embeddings"):
                tail.append(chunk.decode("utf-8", "replace"))
                while len(tail) > 8 or sum(len(c) for c in tail) > 16384:
                    tail.pop(0)
            yield chunk
        _record_usage(model_name, tail)
    except (httpx.TransportError, httpx.DecodingError) as exc:
        # Headers are already sent; the only option is logging and ending the stream.
        logger.warning("gateway upstream stream failed", model=model_name, error=str(exc))
    finally:
        await resp.aclose()


async def _release_instance(instance_id: uuid.UUID, model_name: str) -> None:
    async with _active_lock:
        n = _active_by_instance.get(instance_id, 1) - 1
        if n <= 0:
            _active_by_instance.pop(instance_id, None)
        else:
            _active_by_instance[instance_id] = n
    inferna_instance_active_requests.labels(str(instance_id), model_name).set(max(n, 0))


async def _proxy(
    request: Request, raw_body: bytes, model_name: str, db: AsyncSession
) -> StreamingResponse:
    stream = False
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("application/json"):
        try:
            payload = json.loads(raw_body)
            stream = bool(payload.get("stream")) if isinstance(payload, dict) else False
        except (json.JSONDecodeError, UnicodeDecodeError):
            stream = False

    forwarded_headers: dict[str, str] = {}
    if request.headers.get("content-type"):
        forwarded_headers["content-type"] = request.headers["content-type"]

    client = get_gateway_client(request.app)
    settings = get_settings()
    target, instance, model = await _resolve_target(db, model_name, request)
    async with _active_lock:
        n = _active_by_instance.get(instance.id, 0) + 1
        _active_by_instance[instance.id] = n
        inferna_instance_active_requests.labels(str(instance.id), model_name).set(n)
    # From here on the routing slot must be released exactly once on every path.
    # The success path hands ownership to the StreamingResponse BackgroundTask;
    # the outer finally covers everything else — build/send failures,
    # non-transport errors and asyncio.CancelledError on client disconnect —
    # without swallowing them.
    slot_handed_off = False
    try:
        # Preserve the original hostname in the Host header for virtual hosting,
        # even though the connection target is now a pinned IP (prevents vhost mismatch).
        worker = instance.worker
        assert worker is not None  # guaranteed by _resolve_target
        raw_host = worker.address or worker.hostname
        host_header = _extract_host(raw_host)
        if ":" in host_header and not host_header.startswith("["):
            host_header = f"[{host_header}]"  # bracket IPv6 literals for Host
        forwarded_headers["host"] = f"{host_header}:{instance.port}"
        started: float | None = None
        try:
            req = client.build_request(
                request.method,
                target,
                content=raw_body or None,
                headers=forwarded_headers,
                # httpx 0.28 dropped send(timeout=); per-request timeouts go through build_request.
                timeout=httpx.Timeout(
                    # Streams: no total deadline, but a generous per-read idle
                    # timeout so a stalled upstream cannot hang forever.
                    None if stream else settings.gateway_read_timeout,
                    connect=settings.gateway_connect_timeout,
                    read=STREAM_IDLE_TIMEOUT_SECONDS if stream else settings.gateway_read_timeout,
                ),
            )
            started = time.monotonic()
            resp = await client.send(req, stream=True)
        except (httpx.TransportError, httpx.InvalidURL) as exc:
            elapsed = time.monotonic() - started if started is not None else 0
            inferna_time_to_first_byte_seconds.labels(model_name).observe(elapsed)
            inferna_requests.labels(model=model_name, status="502").inc()
            logger.warning(
                "gateway upstream unreachable", model=model_name, target=target, error=str(exc)
            )
            raise OpenAIError(502, "upstream unreachable", "api_error", "upstream_error") from exc

        inferna_time_to_first_byte_seconds.labels(model_name).observe(time.monotonic() - started)  # type: ignore[arg-type]
        inferna_requests.labels(model=model_name, status=str(resp.status_code)).inc()
        response = StreamingResponse(
            _relay(resp, model_name, request.url.path),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type") or "application/json",
            # Releases the routing slot after the stream ends or the client
            # disconnects mid-stream — more reliable than a finally in _relay.
            background=BackgroundTask(_release_instance, instance.id, model_name),
        )
        slot_handed_off = True
        return response
    finally:
        if not slot_handed_off:
            await _release_instance(instance.id, model_name)
_CHAT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Success (SSE stream when stream=true)",
        "content": {
            "application/json": {"schema": {}},
            "text/event-stream": {
                "schema": {"type": "string", "description": "SSE stream of JSON chunks"}
            },
        },
    },
    400: {"description": "Bad request — missing or invalid model"},
    401: {"description": "Missing/invalid/revoked/inactive API key"},
    403: {"description": "API key lacks inference scope"},
    404: {"description": "Model unknown or no running instance"},
    422: {
        "description": "Validation error",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
            }
        },
    },
    413: {"description": "Request body exceeds size limit"},
    502: {"description": "Upstream unreachable or target not allowed"},
}

_EMBEDDING_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "Success"},
    400: {"description": "Bad request — missing or invalid model"},
    401: {"description": "Missing/invalid/revoked/inactive API key"},
    403: {"description": "API key lacks inference scope"},
    404: {"description": "Model unknown or no running instance"},
    422: {
        "description": "Validation error",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
            }
        },
    },
    413: {"description": "Request body exceeds size limit"},
    502: {"description": "Upstream unreachable or target not allowed"},
}

_TRANSCRIPTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "Success"},
    400: {"description": "Bad request — missing or invalid model"},
    401: {"description": "Missing/invalid/revoked/inactive API key"},
    403: {"description": "API key lacks inference scope"},
    404: {"description": "Model unknown or no running instance"},
    422: {
        "description": "Validation error",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
            }
        },
    },
    413: {"description": "Request body exceeds size limit"},
    502: {"description": "Upstream unreachable or target not allowed"}
}
# Handlers read the raw body and forward it verbatim, so FastAPI cannot infer
# requestBody from the signatures; declare it via openapi_extra so generated
# clients send a body (the gateway rejects missing model with 400 otherwise).
_CHAT_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "messages": {"type": "array", "items": {}},
                    "stream": {"type": "boolean"},
                },
                "required": ["model", "messages"],
            }
        }
    },
}

_EMBEDDINGS_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "input": {"type": "string"},
                    "encoding_format": {"type": "string"},
                },
                "required": ["model", "input"],
            }
        }
    },
}

_TRANSCRIPTION_REQUEST_BODY = {
    "required": True,
    "content": {
        # multipart form upload (OpenAI client default)
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "file": {"type": "string", "format": "binary"},
                },
                "required": ["model", "file"],
            }
        },
        # raw audio bytes; model comes via ?model= query param
        "application/octet-stream": {
            "schema": {"type": "string", "format": "binary"}
        },
    },
}

_LIST_MODELS_RESPONSES = {
    200: {"description": "Success"},
    401: {"description": "Missing/invalid/revoked/inactive API key"},
    403: {"description": "API key lacks inference scope"},
}


@router.post(
    "/chat/completions",
    responses=_CHAT_RESPONSES,
    openapi_extra={"requestBody": _CHAT_REQUEST_BODY},
)  # type: ignore[arg-type]
async def chat_completions(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await _read_body_limited(request, MAX_JSON_BODY_SIZE)
    model_name = await _extract_model(request, raw_body)
    return await _proxy(request, raw_body, model_name, db)


@router.post(
    "/embeddings",
    responses=_EMBEDDING_RESPONSES,
    openapi_extra={"requestBody": _EMBEDDINGS_REQUEST_BODY},
)  # type: ignore[arg-type]
async def embeddings(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await _read_body_limited(request, MAX_JSON_BODY_SIZE)
    model_name = await _extract_model(request, raw_body)
    return await _proxy(request, raw_body, model_name, db)


@router.post(
    "/audio/transcriptions",
    responses=_TRANSCRIPTION_RESPONSES,
    openapi_extra={"requestBody": _TRANSCRIPTION_REQUEST_BODY},
)  # type: ignore[arg-type]
async def audio_transcriptions(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await _read_body_limited(request, MAX_BODY_SIZE)
    model_name = await _extract_model(request, raw_body)
    return await _proxy(request, raw_body, model_name, db)


@router.get("/models", responses=_LIST_MODELS_RESPONSES)  # type: ignore[arg-type]
async def list_models(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ModelInstance)
            .where(ModelInstance.state.in_(LIVE_STATES), ModelInstance.port.isnot(None))
            .join(Model, Model.id == ModelInstance.model_id)
            .options(selectinload(ModelInstance.model))
            .order_by(Model.display_name, ModelInstance.created_at)
        )
    ).scalars().all()
    seen: set[str] = set()
    data: list[dict] = []
    for inst in rows:
        if inst.model is None or inst.model.name in seen:
            continue
        seen.add(inst.model.name)
        data.append(
            {
                "id": inst.model.name,
                "object": "model",
                "created": int(inst.created_at.timestamp()),
                "owned_by": "inferna",
            }
        )
    return {"object": "list", "data": data}
