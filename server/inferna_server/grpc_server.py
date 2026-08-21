"""gRPC WorkerService (aio) + disconnect watchdog, run as asyncio tasks in lifespan."""

from __future__ import annotations

import asyncio

import grpc
import structlog

from inferna_server.config import get_settings
from inferna_server.db import SessionLocal
from inferna_server.proto import cluster_pb2_grpc
from inferna_server.services import workers_svc
from inferna_server.services.metrics import refresh_gauges

logger = structlog.get_logger(__name__)


class WorkerService(cluster_pb2_grpc.WorkerServiceServicer):
    async def Register(self, request, context):  # type: ignore[override]
        try:
            async with SessionLocal() as db:
                return await workers_svc.register_worker(db, request)
        except grpc.RpcError as exc:
            await context.abort(exc.code(), exc.details() or "register failed")
        except Exception as exc:  # noqa: BLE001
            logger.exception("register failed")
            await context.abort(grpc.StatusCode.INTERNAL, "internal error")

    async def Sync(self, request, context):  # type: ignore[override]
        try:
            async with SessionLocal() as db:
                response = await workers_svc.sync_worker(db, request)
                await refresh_gauges(db)
            return response
        except grpc.RpcError as exc:
            await context.abort(exc.code(), exc.details() or "sync failed")
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync failed")
            await context.abort(grpc.StatusCode.INTERNAL, "internal error")

async def disconnect_watchdog() -> None:
    while True:
        await asyncio.sleep(workers_svc.DISCONNECT_CHECK_INTERVAL_SECONDS)
        try:
            async with SessionLocal() as db:
                await asyncio.wait_for(workers_svc.mark_disconnected(db), timeout=30)
        except Exception:  # noqa: BLE001
            logger.exception("disconnect watchdog iteration failed")


async def serve_grpc(app=None) -> None:
    settings = get_settings()
    server = grpc.aio.server()
    cluster_pb2_grpc.add_WorkerServiceServicer_to_server(WorkerService(), server)
    server.add_insecure_port(f"[::]:{settings.grpc_port}")
    await server.start()
    if app is not None:
        app.state.grpc_ready = True
    logger.info("gRPC server started", port=settings.grpc_port)
    watchdog = asyncio.create_task(disconnect_watchdog())
    if app is not None:
        app.state.watchdog = watchdog
    try:
        await server.wait_for_termination()
    finally:
        if app is not None:
            app.state.grpc_ready = False
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
