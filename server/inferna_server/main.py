"""FastAPI app: lifespan (DB seed + gRPC task), CORS, routers, metrics, healthz."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import structlog
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from inferna_server.api import api_router
from inferna_server.auth import hash_password
from inferna_server.config import get_settings
from inferna_server.db import SessionLocal
from inferna_server.grpc_server import serve_grpc
from inferna_server.models import Cluster, User
from inferna_server.services.workers_svc import seed_catalog
from inferna_server.version import SERVER_VERSION

logger = structlog.get_logger(__name__)


async def _check_schema() -> None:
    try:
        async with SessionLocal() as db:
            result = await db.execute(text("SELECT version_num FROM alembic_version"))
            row = result.fetchone()
            current = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("database schema missing — run `alembic upgrade head`") from exc
    if current is None:
        raise RuntimeError("database schema missing — run `alembic upgrade head`")
    # get heads
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    try:
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"failed to read alembic heads: {exc}") from exc
    if current not in heads:
        raise RuntimeError(f"database schema {current} is not up to date (heads: {heads}) — run `alembic upgrade head`")


async def readiness_loop(app: FastAPI) -> None:
    # First iteration immediately, then every 30s
    while True:
        ready = True
        reason: str | None = None
        # (a) DB check
        try:
            async with SessionLocal() as db:
                await db.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            ready = False
            reason = f"db check failed: {exc}"
        # (b) schema version
        if ready:
            try:
                async with SessionLocal() as db:
                    result = await db.execute(text("SELECT version_num FROM alembic_version"))
                    row = result.fetchone()
                    current = row[0] if row else None
                config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
                heads = ScriptDirectory.from_config(config).get_heads()
                if current not in heads:
                    ready = False
                    reason = f"schema {current} not in heads {heads}"
            except Exception as exc:  # noqa: BLE001
                ready = False
                reason = f"schema check failed: {exc}"
        # (c) gRPC ready
        if ready:
            if not getattr(app.state, "grpc_ready", False):
                ready = False
                reason = "gRPC not ready"
        app.state.ready = (ready, reason)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _check_schema()
    await _seed()
    app.state.grpc_ready = False
    app.state.ready = (False, "starting")
    grpc_task = asyncio.create_task(serve_grpc(app))
    readiness_task = asyncio.create_task(readiness_loop(app))
    app.state.grpc_task = grpc_task
    app.state.readiness_task = readiness_task
    logger.info("startup complete", grpc_port=get_settings().grpc_port)
    try:
        yield
    finally:
        for t in (grpc_task, readiness_task):
            t.cancel()
            with suppress(asyncio.CancelledError):
                try:
                    await asyncio.wait_for(t, timeout=5)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    pass


async def _seed() -> None:
    settings = get_settings()
    async with SessionLocal() as db:
        admin = (await db.execute(select(User).limit(1))).scalar_one_or_none()
        if admin is None:
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                    is_active=True,
                )
            )
            logger.info("seeded admin user")
        cluster = (
            await db.execute(select(Cluster).where(Cluster.name == "default"))
        ).scalar_one_or_none()
        if cluster is None:
            db.add(Cluster(name="default", description="Default cluster"))
            logger.info("seeded default cluster")
        await seed_catalog(db)
        await db.commit()


settings = get_settings()
app = FastAPI(title="Inferna Next", version=SERVER_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request):
    ready, reason = getattr(request.app.state, "ready", (False, "not initialized"))
    if ready:
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"status": "not ready", "reason": reason})
