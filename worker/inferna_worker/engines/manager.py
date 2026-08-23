"""Engine container reconciliation: pull/create/start/health-probe/stop/remove.

Real mode drives Docker via the `docker` SDK. Docker calls are blocking
(image pulls can take minutes), so they run in a worker thread while the
async Sync loop keeps the worker alive; the health probe runs as an asyncio
task scheduled from the loop. Mock mode keeps an in-memory registry and never
touches Docker.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from contextlib import suppress
from typing import Any

import httpx
import structlog
from docker.types import DeviceRequest

from inferna_worker.config import Settings
from inferna_worker.engines.base import build_command, image_for
from inferna_worker.proto import cluster_pb2

logger = structlog.get_logger(__name__)

CONTAINER_PREFIX = "inferna-instance-"
LABEL_MANAGED = "inferna.managed"
LABEL_INSTANCE_ID = "inferna.instance_id"
LABEL_GENERATION = "inferna.generation"
LABEL_PORT = "inferna.port"
HEALTH_PROBE_INTERVAL_SECONDS = 10
HEALTH_PROBE_TIMEOUT_SECONDS = 2400
IMAGE_PULL_TIMEOUT_SECONDS = 1800
CONTAINER_OP_TIMEOUT_SECONDS = 120
STOP_TIMEOUT_SECONDS = 60
MOCK_START_DELAY_SECONDS = 2
LOG_TAIL_LINES = 20


class InstanceManager:
    def __init__(self, settings: Settings, docker_client: Any | None = None) -> None:
        self.settings = settings
        self.mock = settings.mock_engine
        self._docker = docker_client
        self._pulled_images: set[str] = set()
        # instance_id -> {"state", "port", "detail", "started_at", "generation"}
        self._instances: dict[str, dict[str, Any]] = {}
        self._probe_tasks: dict[str, asyncio.Task] = {}
        self._pending_probes: list[tuple[str, int]] = []
        self._batch_task: asyncio.Task[None] | None = None
        self._pending: dict[str, cluster_pb2.InstanceCommand] = {}

    @property
    def docker(self) -> Any:
        if self._docker is None and not self.mock:
            import docker

            self._docker = docker.from_env()
        return self._docker

    # --- lifecycle ---

    async def startup_cleanup(self) -> None:
        """Backward compat alias for adopt_existing."""
        await self.adopt_existing()

    async def adopt_existing(self) -> None:
        """Adopt existing managed containers and clean legacy unmanaged ones."""
        if self.mock:
            return
        await asyncio.to_thread(self._adopt_existing_sync)

    def _adopt_existing_sync(self) -> None:
        try:
            # Adopt managed containers
            try:
                managed = self.docker.containers.list(
                    all=True, filters={"label": f"{LABEL_MANAGED}=true"}
                )
            except TypeError:
                # FakeDocker may not support filters arg
                managed = [
                    c
                    for c in self.docker.containers.list(all=True)
                    if getattr(c, "labels", {}).get(LABEL_MANAGED) == "true"
                ]
            for container in managed:
                try:
                    name = getattr(container, "name", "")
                    if not name.startswith(CONTAINER_PREFIX):
                        continue
                    instance_id = name[len(CONTAINER_PREFIX):]
                    labels = getattr(container, "labels", {}) or {}
                    # status may be "running", "exited", etc.
                    c_status = getattr(container, "status", "running")
                    state = "running" if c_status == "running" else "stopped"
                    try:
                        port = int(labels.get(LABEL_PORT, 0) or 0)
                    except (ValueError, TypeError):
                        port = 0
                    try:
                        generation = int(labels.get(LABEL_GENERATION, 0) or 0)
                    except (ValueError, TypeError):
                        generation = 0
                    self._instances[instance_id] = {
                        "state": state,
                        "port": port,
                        "generation": generation,
                        "detail": "",
                        "started_at": time.monotonic(),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.warning("adopt managed container failed", error=str(exc))
            # Remove legacy unmanaged containers with prefix but without managed label
            for container in self.docker.containers.list(all=True):
                try:
                    name = getattr(container, "name", "")
                    if not name.startswith(CONTAINER_PREFIX):
                        continue
                    labels = getattr(container, "labels", {}) or {}
                    if labels.get(LABEL_MANAGED) == "true":
                        continue
                    logger.info("removing legacy container", name=name)
                    container.remove(force=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("legacy removal failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("adopt existing failed", error=str(exc))

    def _startup_cleanup_sync(self) -> None:
        # Kept for backward compat but not used; delegates to adopt
        self._adopt_existing_sync()

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
                        # keep generation as is
                elif command.action == "delete":
                    self._instances.pop(command.instance_id, None)
            return self.statuses()
        # Real mode: coalesce into pending if batch is running
        if self._batch_task is not None and not self._batch_task.done():
            for cmd in commands:
                self._pending[cmd.instance_id] = cmd
            return self.statuses()
        # otherwise start new batch
        self._batch_task = asyncio.create_task(self._apply_batch(commands))
        return self.statuses()

    async def _apply_batch(self, commands: list[cluster_pb2.InstanceCommand]) -> None:
        try:
            for command in commands:
                try:
                    await asyncio.to_thread(self._apply_docker_command, command)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "command failed", instance_id=command.instance_id, error=str(exc)
                    )
                    with suppress(Exception):
                        self._instances[command.instance_id] = {
                            "state": "error",
                            "port": command.config.port if command.HasField("config") else 0,
                            "detail": str(exc),
                            "generation": command.generation,
                            "started_at": time.monotonic(),
                        }
            await asyncio.to_thread(self._remove_stale_containers_sync)
            self._schedule_pending_probes()
        except Exception:  # noqa: BLE001
            logger.exception("command batch failed")
        finally:
            self._batch_task = None
            if self._pending:
                pending = list(self._pending.values())
                self._pending = {}
                self._batch_task = asyncio.create_task(self._apply_batch(pending))
    def _apply_docker_command(self, command: cluster_pb2.InstanceCommand) -> None:
        try:
            if command.action == "start":
                self._start_sync(command)
            elif command.action == "stop":
                self._stop_sync(command.instance_id)
            elif command.action == "delete":
                self._delete_sync(command.instance_id)
            else:
                logger.warning("unknown command action", action=command.action)
        except Exception as exc:  # noqa: BLE001
            logger.exception("apply docker command failed", action=command.action, error=str(exc))
            # Convert to error state ensuring generation recorded
            with suppress(Exception):
                self._instances[command.instance_id] = {
                    "state": "error",
                    "port": command.config.port if command.HasField("config") else 0,
                    "detail": str(exc),
                    "generation": command.generation,
                    "started_at": time.monotonic(),
                }

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
                "generation": command.generation,
            }
            return
        self._instances[instance_id] = {
            "state": "starting",
            "port": config.port,
            "detail": "",
            "started_at": time.monotonic(),
            "generation": command.generation,
        }

    # --- commands (real, run in a thread) ---

    def _start_sync(self, command: cluster_pb2.InstanceCommand) -> None:
        instance_id = command.instance_id
        config = command.config
        # Idempotency: skip if already starting/running with same or higher generation
        tracked = self._instances.get(instance_id)
        if (
            tracked is not None
            and tracked.get("state") in ("starting", "running")
            and tracked.get("generation", 0) >= command.generation
        ):
            return
        # Lower generation or stopped/error: remove old container before recreating
        if tracked is not None and (
            tracked.get("generation", 0) < command.generation
            or tracked.get("state") in ("stopped", "error")
        ):
            try:
                container = self.docker.containers.get(
                    f"{CONTAINER_PREFIX}{instance_id}"
                )
                container.remove(force=True)
            except Exception:
                pass
        if config.requires_hf_token and not self.settings.hf_token:
            self._instances[instance_id] = {
                "state": "error",
                "port": config.port,
                "detail": "model requires HF token",
                "started_at": time.monotonic(),
                "generation": command.generation,
            }
            return
        try:
            docker_client = self.docker
            image = image_for(config.engine, self.settings)
            if image not in self._pulled_images:
                logger.info("pulling engine image", image=image)
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(docker_client.images.pull, image)
                        future.result(timeout=IMAGE_PULL_TIMEOUT_SECONDS)
                except concurrent.futures.TimeoutError:
                    self._instances[instance_id] = {
                        "state": "error",
                        "port": config.port,
                        "detail": "image pull timed out",
                        "started_at": time.monotonic(),
                        "generation": command.generation,
                    }
                    logger.warning("image pull timed out", instance_id=instance_id, image=image)
                    return
                self._pulled_images.add(image)
            environment = {}
            if self.settings.hf_token:
                environment["HF_HUB_TOKEN"] = self.settings.hf_token
            labels = {
                LABEL_MANAGED: "true",
                LABEL_INSTANCE_ID: instance_id,
                LABEL_GENERATION: str(command.generation),
                LABEL_PORT: str(config.port),
            }
            container = docker_client.containers.create(
                image,
                name=f"{CONTAINER_PREFIX}{instance_id}",
                entrypoint=[],
                labels=labels,
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
                "generation": command.generation,
            }
            self._pending_probes.append((instance_id, config.port))
            logger.info("instance started", instance_id=instance_id, port=config.port)
        except Exception as exc:  # noqa: BLE001
            self._instances[instance_id] = {
                "state": "error",
                "port": config.port,
                "detail": f"docker unavailable: {exc}",
                "started_at": time.monotonic(),
                "generation": command.generation,
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
            # keep generation
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
        """Remove managed containers not in _instances (server-side deletes)."""
        try:
            try:
                containers = self.docker.containers.list(
                    all=True, filters={"label": f"{LABEL_MANAGED}=true"}
                )
            except TypeError:
                containers = [
                    c
                    for c in self.docker.containers.list(all=True)
                    if getattr(c, "labels", {}).get(LABEL_MANAGED) == "true"
                ]
            for container in containers:
                name = getattr(container, "name", "")
                if not name.startswith(CONTAINER_PREFIX):
                    continue
                instance_id = name[len(CONTAINER_PREFIX):]
                if instance_id not in self._instances:
                    logger.info("removing stale container", name=name)
                    container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale container sweep failed", error=str(exc))

    # --- health probe / reporting ---

    async def _health_probe(self, instance_id: str, port: int) -> None:
        url = f"http://127.0.0.1:{port}/v1/models"
        deadline = time.monotonic() + HEALTH_PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # Fast path: check if container exited
            if not self.mock:
                try:
                    container = await asyncio.to_thread(
                        self.docker.containers.get, f"{CONTAINER_PREFIX}{instance_id}"
                    )
                    c_status = getattr(container, "status", "")
                    if c_status in ("exited", "dead"):
                        info = self._instances.get(instance_id)
                        if info is not None:
                            tail = await asyncio.to_thread(self._container_log_tail, instance_id)
                            info["state"] = "error"
                            info["detail"] = tail or f"container {c_status}"
                        logger.warning("container exited", instance_id=instance_id, status=c_status)
                        return
                except Exception:
                    pass
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
            # second check after request
            if not self.mock:
                try:
                    container = await asyncio.to_thread(
                        self.docker.containers.get, f"{CONTAINER_PREFIX}{instance_id}"
                    )
                    c_status = getattr(container, "status", "")
                    if c_status in ("exited", "dead"):
                        info = self._instances.get(instance_id)
                        if info is not None:
                            tail = await asyncio.to_thread(self._container_log_tail, instance_id)
                            info["state"] = "error"
                            info["detail"] = tail or f"container {c_status}"
                        return
                except Exception:
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
                    generation=info.get("generation", 0),
                )
            )
        return result

    async def shutdown(self) -> None:
        if self._batch_task is not None:
            self._batch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._batch_task
        for task in list(self._probe_tasks.values()):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._probe_tasks.clear()
        self._pending_probes.clear()
        if not self.mock:
            try:
                # docker client may have close method
                close = getattr(self.docker, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
