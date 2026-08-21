"""FastAPI app: lifespan (DB seed + gRPC task), CORS, routers, metrics, healthz."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from inferna_server.api import api_router
from inferna_server.auth import hash_password
from inferna_server.config import get_settings
from inferna_server.db import SessionLocal
from inferna_server.grpc_server import serve_grpc
from inferna_server.models import Cluster, User
from inferna_server.services.workers_svc import seed_catalog
from inferna_server.version import SERVER_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _seed()
    grpc_task = asyncio.create_task(serve_grpc())
    logger.info("startup complete", grpc_port=get_settings().grpc_port)
    try:
        yield
    finally:
        grpc_task.cancel()
        with suppress(asyncio.CancelledError):
            await grpc_task


async def _seed() -> None:
    settings = get_settings()
    try:
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
    except OperationalError:
        logger.warning("database tables missing — run `alembic upgrade head`; skipping seed")
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
