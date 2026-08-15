"""Engine container reconciliation: pull/create/start/health-probe/stop/remove.

Real mode drives Docker via the `docker` SDK. Docker calls are blocking
(image pulls can take minutes), so they run in a worker thread while the
async Sync loop keeps the worker alive; the health probe runs as an asyncio
task scheduled from the loop. Mock mode keeps an in-memory registry and never
touches Docker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog
from docker.types import DeviceRequest

from inferna_worker.config import Settings
from inferna_worker.engines.base import build_command, image_for
from inferna_worker.proto import cluster_pb2

logger = structlog.get_logger(__name__)

CONTAINER_PREFIX = "inferna-instance-"
HEALTH_PROBE_INTERVAL_SECONDS = 10
HEALTH_PROBE_TIMEOUT_SECONDS = 600
MOCK_START_DELAY_SECONDS = 2
LOG_TAIL_LINES = 20


class InstanceManager:
    def __init__(self, settings: Settings, docker_client: Any | None = None) -> None:
        self.settings = settings
        self.mock = settings.mock_engine
        self._docker = docker_client
        self._pulled_images: set[str] = set()
        # instance_id -> {"state", "port", "detail", "started_at"}
        self._instances: dict[str, dict[str, Any]] = {}
        self._probe_tasks: dict[str, asyncio.Task] = {}
        self._pending_probes: list[tuple[str, int]] = []
        self._applying = False

    @property
    def docker(self) -> Any:
        if self._docker is None and not self.mock:
            import docker

            self._docker = docker.from_env()
        return self._docker

    # --- lifecycle ---

    async def startup_cleanup(self) -> None:
        """Crash recovery: remove orphaned inferna-instance-* containers."""
        if self.mock:
            return
        await asyncio.to_thread(self._startup_cleanup_sync)

    def _startup_cleanup_sync(self) -> None:
        try:
            for container in self.docker.containers.list(all=True):
                if container.name.startswith(CONTAINER_PREFIX):
                    logger.info("removing orphaned container", name=container.name)
                    container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("startup cleanup failed", error=str(exc))

    async def reconcile(
        self, commands: list[cluster_pb2.InstanceCommand]
    ) -> list[cluster_pb2.InstanceStatus]:
        if self.mock:
            for command in commands:
                if command.action == "start":
                    await self._start_mock(command)
                elif command.action == "stop":
                    if command.instance_id in self._instances:
                        self._instances[command.instance_id]["state"] = "stopped"
                        self._instances[command.instance_id]["detail"] = ""
                elif command.action == "delete":
                    self._instances.pop(command.instance_id, None)
            return self.statuses()
        # Real mode: command application (image pulls!) can take minutes. Run
        # it as a background task so the Sync loop keeps reporting and the
        # server never marks the worker disconnected. One batch at a time:
        # commands are recomputed from desired state each Sync, so a skipped
        # duplicate is re-issued on the next cycle.
        if not self._applying:
            self._applying = True
            asyncio.create_task(self._apply_batch(commands))
        return self.statuses()

    async def _apply_batch(self, commands: list[cluster_pb2.InstanceCommand]) -> None:
        try:
            for command in commands:
                await asyncio.to_thread(self._apply_docker_command, command)
            await asyncio.to_thread(self._remove_stale_containers_sync)
            self._schedule_pending_probes()
        except Exception:  # noqa: BLE001
            logger.exception("command batch failed")
        finally:
            self._applying = False

    def _apply_docker_command(self, command: cluster_pb2.InstanceCommand) -> None:
        if command.action == "start":
            self._start_sync(command)
        elif command.action == "stop":
            self._stop_sync(command.instance_id)
        elif command.action == "delete":
            self._delete_sync(command.instance_id)
        else:
            logger.warning("unknown command action", action=command.action)

    def _schedule_pending_probes(self) -> None:
        for instance_id, port in self._pending_probes:
            self._probe_tasks[instance_id] = asyncio.create_task(
                self._health_probe(instance_id, port)
            )
        self._pending_probes.clear()

    # --- commands (mock) ---

    async def _start_mock(self, command: cluster_pb2.InstanceCommand) -> None:
        instance_id = command.instance_id
        config = command.config
        if config.requires_hf_token and not self.settings.hf_token:
            self._instances[instance_id] = {
                "state": "error",
                "port": config.port,
                "detail": "model requires HF token",
                "started_at": time.monotonic(),
            }
            return
        self._instances[instance_id] = {
            "state": "starting",
            "port": config.port,
            "detail": "",
            "started_at": time.monotonic(),
        }

    # --- commands (real, run in a thread) ---

    def _start_sync(self, command: cluster_pb2.InstanceCommand) -> None:
        instance_id = command.instance_id
        config = command.config
        # Idempotency: a duplicate start command (re-issued while a previous
        # batch is still applying) must not create a second container.
        tracked = self._instances.get(instance_id)
        if tracked is not None and tracked.get("state") in ("starting", "running", "stopped"):
            return
        if config.requires_hf_token and not self.settings.hf_token:
            self._instances[instance_id] = {
                "state": "error",
                "port": config.port,
                "detail": "model requires HF token",
                "started_at": time.monotonic(),
            }
            return
        try:
            docker_client = self.docker
            image = image_for(config.engine, self.settings)
            if image not in self._pulled_images:
                logger.info("pulling engine image", image=image)
                docker_client.images.pull(image)
                self._pulled_images.add(image)
            environment = {}
            if self.settings.hf_token:
                environment["HF_HUB_TOKEN"] = self.settings.hf_token
            container = docker_client.containers.create(
                image,
                name=f"{CONTAINER_PREFIX}{instance_id}",
                entrypoint=[],
                device_requests=[
                    DeviceRequest(
                        device_ids=[str(i) for i in config.gpu_indexes],
                        capabilities=[["gpu"]],
                    )
                ],
                ports={"8000/tcp": config.port},
                environment=environment,
                volumes={
                    self.settings.models_dir: {
                        "bind": "/root/.cache/huggingface",
                        "mode": "rw",
                    }
                },
                command=build_command(config),
            )
            container.start()
            self._instances[instance_id] = {
                "state": "starting",
                "port": config.port,
                "detail": "",
                "started_at": time.monotonic(),
            }
            self._pending_probes.append((instance_id, config.port))
            logger.info("instance started", instance_id=instance_id, port=config.port)
        except Exception as exc:  # noqa: BLE001
            self._instances[instance_id] = {
                "state": "error",
                "port": config.port,
                "detail": f"docker unavailable: {exc}",
                "started_at": time.monotonic(),
            }
            logger.warning("instance start failed", instance_id=instance_id, error=str(exc))

    def _stop_sync(self, instance_id: str) -> None:
        probe = self._probe_tasks.pop(instance_id, None)
        if probe is not None:
            probe.cancel()
        try:
            container = self.docker.containers.get(f"{CONTAINER_PREFIX}{instance_id}")
            container.stop(timeout=10)
        except Exception:  # noqa: BLE001  (NotFound included: nothing to stop)
            pass
        if instance_id in self._instances:
            self._instances[instance_id]["state"] = "stopped"
            self._instances[instance_id]["detail"] = ""

    def _delete_sync(self, instance_id: str) -> None:
        probe = self._probe_tasks.pop(instance_id, None)
        if probe is not None:
            probe.cancel()
        try:
            container = self.docker.containers.get(f"{CONTAINER_PREFIX}{instance_id}")
            container.remove(force=True)
        except Exception:  # noqa: BLE001  (NotFound included)
            pass
        self._instances.pop(instance_id, None)

    def _remove_stale_containers_sync(self) -> None:
        """Remove tracked-set leftovers (server-side deletes covered)."""
        try:
            for container in self.docker.containers.list(all=True):
                if not container.name.startswith(CONTAINER_PREFIX):
                    continue
                instance_id = container.name[len(CONTAINER_PREFIX) :]
                if instance_id not in self._instances:
                    logger.info("removing stale container", name=container.name)
                    container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale container sweep failed", error=str(exc))

    # --- health probe / reporting ---

    async def _health_probe(self, instance_id: str, port: int) -> None:
        url = f"http://127.0.0.1:{port}/v1/models"
        deadline = time.monotonic() + HEALTH_PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(url)
                if response.status_code == 200:
                    info = self._instances.get(instance_id)
                    if info is not None:
                        info["state"] = "running"
                        info["detail"] = ""
                    logger.info("instance healthy", instance_id=instance_id, port=port)
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(HEALTH_PROBE_INTERVAL_SECONDS)
        info = self._instances.get(instance_id)
        if info is not None:
            info["state"] = "error"
            info["detail"] = (
                await asyncio.to_thread(self._container_log_tail, instance_id)
            ) or "health probe timed out"

    def _container_log_tail(self, instance_id: str) -> str:
        if self.mock:
            return ""
        try:
            container = self.docker.containers.get(f"{CONTAINER_PREFIX}{instance_id}")
            logs = container.logs(tail=LOG_TAIL_LINES)
            return (logs.decode(errors="replace") if isinstance(logs, bytes) else str(logs)).strip()
        except Exception:  # noqa: BLE001
            return ""

    def statuses(self) -> list[cluster_pb2.InstanceStatus]:
        now = time.monotonic()
        result: list[cluster_pb2.InstanceStatus] = []
        for instance_id, info in self._instances.items():
            state = info["state"]
            elapsed = now - info.get("started_at", now)
            if self.mock and state == "starting" and elapsed >= MOCK_START_DELAY_SECONDS:
                state = "running"
                info["state"] = "running"
            result.append(
                cluster_pb2.InstanceStatus(
                    instance_id=instance_id,
                    state=state,
                    detail=info.get("detail", ""),
                    port=info.get("port") or 0,
                )
            )
        return result
