"""OpenAI-compatible inference gateway: API-key auth, upstream resolution, SSE pass-through."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    inferna_requests,
    inferna_time_to_first_byte_seconds,
    inferna_tokens,
)
from inferna_server.services.workers_svc import sha256_hex

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["gateway"])

_USAGE_RE = re.compile(r'"usage"\s*:\s*\{[^}]*\}')
_PROMPT_TOKENS_RE = re.compile(r'"prompt_tokens"\s*:\s*(\d+)')
_COMPLETION_TOKENS_RE = re.compile(r'"completion_tokens"\s*:\s*(\d+)')
# How often a key's last_used_at is stamped; avoids a DB write on every request.
LAST_USED_UPDATE_SECONDS = 60

_DEFAULT_CLIENT: httpx.AsyncClient | None = None


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
    return openai_error(exc.status_code, exc.message, exc.type_, exc.code, headers=exc.headers)


async def get_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
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
        key.last_used_at = utcnow()
        # Commit before any streaming so this write is never blocked behind a long-lived stream.
        await db.commit()
    return key


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
    parser.write(raw_body)
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
        model = _extract_multipart_model(content_type, raw_body)
    if model is None:
        model = request.query_params.get("model")
    if not model:
        raise OpenAIError(
            400, "missing 'model' field", "invalid_request_error", "missing_model"
        )
    return model


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
    # Deterministic: oldest live instance when several exist (no load balancing in Phase 1).
    instance = (
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
        .first()
    )
    if instance is None or instance.worker is None:
        raise OpenAIError(
            404,
            f"model '{model_name}' has no running instance",
            "invalid_request_error",
            "model_not_found",
        )
    # Worker engines expose the OpenAI-compatible surface under /v1 (vLLM/SGLang),
    # so the gateway forwards the client path including its /v1 prefix unchanged.
    target = (
        f"http://{instance.worker.address or instance.worker.hostname}:"
        f"{instance.port}{request.url.path}"
    )
    if request.url.query:
        target += f"?{request.url.query}"
    return target, instance, model


def get_gateway_client(app: FastAPI) -> httpx.AsyncClient:
    """Lifespan client when present; else a lazy module-level default.

    Tests override it explicitly via `app.state.gateway_client`.
    """
    client = getattr(app.state, "gateway_client", None)
    if client is not None:
        return client
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        settings = get_settings()
        _DEFAULT_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.gateway_connect_timeout, read=settings.gateway_read_timeout
            )
        )
    return _DEFAULT_CLIENT


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
    # Small tail buffer for usage extraction on chat completions; bytes are always yielded first.
    tail: list[str] = []
    try:
        async for chunk in resp.aiter_bytes():
            if path == "/v1/chat/completions":
                tail.append(chunk.decode("utf-8", "replace"))
                if len(tail) > 2:
                    tail = tail[-2:]
            yield chunk
        _record_usage(model_name, tail)
    except httpx.TransportError as exc:
        # Headers are already sent; the only option is logging and ending the stream.
        logger.warning("gateway upstream stream failed", model=model_name, error=str(exc))
    finally:
        await resp.aclose()


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
    started = time.monotonic()
    try:
        req = client.build_request(
            request.method,
            target,
            content=raw_body or None,
            headers=forwarded_headers,
            # httpx 0.28 dropped send(timeout=); per-request timeouts go through build_request.
            timeout=httpx.Timeout(
                None if stream else settings.gateway_read_timeout,
                connect=settings.gateway_connect_timeout,
            ),
        )
        resp = await client.send(req, stream=True)
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        logger.warning(
            "gateway upstream unreachable", model=model_name, target=target, error=str(exc)
        )
        raise OpenAIError(502, "upstream unreachable", "api_error", "upstream_error") from exc

    inferna_time_to_first_byte_seconds.labels(model_name).observe(time.monotonic() - started)
    inferna_requests.labels(model=model_name, status=str(resp.status_code)).inc()

    return StreamingResponse(
        _relay(resp, model_name, request.url.path),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type") or "application/json",
    )


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    model_name = await _extract_model(request, raw_body)
    return await _proxy(request, raw_body, model_name, db)


@router.post("/embeddings")
async def embeddings(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    model_name = await _extract_model(request, raw_body)
    return await _proxy(request, raw_body, model_name, db)


@router.post("/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    model_name = await _extract_model(request, raw_body)
    return await _proxy(request, raw_body, model_name, db)


@router.get("/models")
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
