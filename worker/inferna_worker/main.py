"""Worker agent: register → Sync loop (5 s) → reconcile; graceful shutdown."""

from __future__ import annotations

import asyncio
import platform
import signal
from contextlib import suppress

import grpc
import psutil
import structlog
from structlog.dev import ConsoleRenderer

from inferna_worker.config import Settings, get_settings
from inferna_worker.engines.manager import InstanceManager
from inferna_worker.gpu import detect
from inferna_worker.proto import cluster_pb2, cluster_pb2_grpc
from inferna_worker.version import PROTOCOL_VERSION, VERSION

logger = structlog.get_logger(__name__)

DEFAULT_SYNC_INTERVAL_SECONDS = 5
MAX_BACKOFF_SECONDS = 60


def configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level.upper()),
    )


def _channel_for(settings: Settings) -> grpc.aio.Channel:
    """Plaintext by default (the documented private-network assumption), TLS when a CA is set.

    The CA file is what makes the worker verify the server; `server_tls_authority` only overrides
    the name it checks when the certificate is issued for a host the worker cannot resolve.
    """
    if not settings.server_tls_ca:
        return grpc.aio.insecure_channel(settings.server_url)
    with open(settings.server_tls_ca, "rb") as ca_file:
        credentials = grpc.ssl_channel_credentials(root_certificates=ca_file.read())
    options = []
    if settings.server_tls_authority:
        options.append(("grpc.ssl_target_name_override", settings.server_tls_authority))
    return grpc.aio.secure_channel(settings.server_url, credentials, options=options)


def system_info(settings: Settings) -> cluster_pb2.SystemInfo:
    return cluster_pb2.SystemInfo(
        cpu_cores=psutil.cpu_count(logical=True) or 0,
        memory_mb=psutil.virtual_memory().total // (1024 * 1024),
        os=platform.system().lower(),
        hostname=settings.worker_hostname,
    )


def gpu_infos(gpus) -> list[cluster_pb2.GPUInfo]:
    return [
        cluster_pb2.GPUInfo(
            index=gpu.index,
            vendor=gpu.vendor,
            name=gpu.name,
            vram_mb=gpu.vram_mb,
            used_vram_mb=gpu.used_vram_mb,
            utilization_pct=gpu.utilization_pct,
            uuid=gpu.uuid,
            driver_version=gpu.driver_version,
        )
        for gpu in gpus
    ]


async def run_loop(settings, manager: InstanceManager) -> None:
    channel = _channel_for(settings)
    stub = cluster_pb2_grpc.WorkerServiceStub(channel)
    try:
        # --- register (with backoff) ---
        backoff = 5
        worker_id: str | None = None
        worker_token: str | None = None
        sync_interval = DEFAULT_SYNC_INTERVAL_SECONDS
        while worker_id is None:
            try:
                response = await stub.Register(
                    cluster_pb2.RegisterRequest(
                        cluster_token=settings.registration_token,
                        hostname=settings.worker_hostname,
                        worker_name=settings.worker_name,
                        cluster_name=settings.cluster_name,
                        version=VERSION,
                        protocol_version=PROTOCOL_VERSION,
                        address=settings.worker_address or "",
                    )
                )
                worker_id = response.worker_id
                worker_token = response.worker_token
                sync_interval = response.sync_interval_seconds or DEFAULT_SYNC_INTERVAL_SECONDS
                logger.info("registered with server", worker_id=worker_id, interval=sync_interval)
            except grpc.RpcError as exc:
                if exc.code() == grpc.StatusCode.FAILED_PRECONDITION:
                    logger.error(
                        "server rejected worker", code=exc.code().name, detail=exc.details()
                    )
                    raise SystemExit(1) from exc
                logger.warning("register failed, retrying", code=exc.code().name, backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        # --- sync loop ---
        tick = 0
        backoff = 5
        while True:
            try:
                request = cluster_pb2.SyncRequest(
                    worker_id=worker_id,
                    worker_token=worker_token,
                    system=system_info(settings),
                    gpus=gpu_infos(detect(mock=settings.mock_engine, tick=tick)),
                    instances=await manager.statuses(),
                )
                response = await stub.Sync(request)
                await manager.reconcile(response.commands)
                backoff = 5
            except grpc.RpcError as exc:
                logger.warning("sync failed, retrying", code=exc.code().name, backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("sync iteration failed")
            await asyncio.sleep(sync_interval)
            tick += 1
    finally:
        await channel.close()

async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "inferna worker starting",
        server=settings.server_url,
        mock=settings.mock_engine,
        hostname=settings.worker_hostname,
    )

    manager = InstanceManager(settings)
    await manager.adopt_existing()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):  # e.g. SIGTERM on Windows
            loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(run_loop(settings, manager))
    await stop.wait()
    logger.info("shutdown signal received")
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await manager.shutdown()
    logger.info("worker stopped")
