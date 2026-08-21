"""Mock-mode reconcile tests + real-mode orphan cleanup (fake docker client)."""

from __future__ import annotations

import time

from inferna_worker.config import Settings
from inferna_worker.engines.manager import CONTAINER_PREFIX, InstanceManager
from inferna_worker.proto import cluster_pb2

MOCK_SETTINGS = Settings(mock_engine=True)


def _start_command(
    instance_id: str = "abc", requires_hf_token: bool = False, generation: int = 1
) -> cluster_pb2.InstanceCommand:
    return cluster_pb2.InstanceCommand(
        instance_id=instance_id,
        action="start",
        generation=generation,
        config=cluster_pb2.EngineConfig(
            engine="vllm",
            model_name="Qwen/Qwen2.5-0.5B-Instruct",
            profile="latency",
            gpu_indexes=[0],
            vram_required_mb=2048,
            port=8010,
            requires_hf_token=requires_hf_token,
        ),
    )


def _stop_command(instance_id: str) -> cluster_pb2.InstanceCommand:
    return cluster_pb2.InstanceCommand(instance_id=instance_id, action="stop")


def _delete_command(instance_id: str) -> cluster_pb2.InstanceCommand:
    return cluster_pb2.InstanceCommand(instance_id=instance_id, action="delete")


def _status_map(
    statuses: list[cluster_pb2.InstanceStatus],
) -> dict[str, cluster_pb2.InstanceStatus]:
    return {s.instance_id: s for s in statuses}


async def test_mock_start_goes_running_after_delay() -> None:
    manager = InstanceManager(MOCK_SETTINGS)
    statuses = await manager.reconcile([_start_command()])
    assert statuses[0].state == "starting"
    assert statuses[0].port == 8010

    # Simulate elapsed time >= 2 s.
    manager._instances["abc"]["started_at"] = time.monotonic() - 5
    statuses = manager.statuses()
    assert statuses[0].state == "running"


async def test_mock_stop() -> None:
    manager = InstanceManager(MOCK_SETTINGS)
    await manager.reconcile([_start_command()])
    await manager.reconcile([_stop_command("abc")])
    statuses = _status_map(manager.statuses())
    assert statuses["abc"].state == "stopped"


async def test_mock_delete_removes_instance() -> None:
    manager = InstanceManager(MOCK_SETTINGS)
    await manager.reconcile([_start_command()])
    await manager.reconcile([_delete_command("abc")])
    assert manager.statuses() == []


async def test_mock_requires_hf_token_error() -> None:
    manager = InstanceManager(Settings(mock_engine=True, hf_token=""))
    statuses = await manager.reconcile([_start_command(requires_hf_token=True)])
    assert statuses[0].state == "error"
    assert statuses[0].detail == "model requires HF token"


async def test_mock_hf_token_present_allows_start() -> None:
    manager = InstanceManager(Settings(mock_engine=True, hf_token="hf_secret"))
    statuses = await manager.reconcile([_start_command(requires_hf_token=True)])
    assert statuses[0].state == "starting"


class FakeContainer:
    def __init__(self, name: str, labels: dict | None = None, status: str = "running") -> None:
        self.name = name
        self.labels = labels or {}
        self.status = status
        self.removed = False

    def remove(self, force: bool = False) -> None:  # noqa: ARG002
        self.removed = True

    def stop(self, timeout: int = 10) -> None:  # noqa: ARG002
        self.status = "exited"

    def start(self) -> None:
        self.status = "running"

    def logs(self, tail: int = 20) -> bytes:  # noqa: ARG002
        return b"fake log"


class FakeDockerImages:
    def pull(self, image: str) -> None:  # noqa: ARG002
        return None


class FakeDockerContainers:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self.containers = containers

    def list(self, all: bool = False, filters: dict | None = None):  # noqa: A002, ARG002
        if filters and "label" in filters:
            label_filter = filters["label"]
            # format "inferna.managed=true"
            if "=" in label_filter:
                k, v = label_filter.split("=", 1)
                return [c for c in self.containers if c.labels.get(k) == v]
            return [c for c in self.containers if label_filter in c.labels]
        return self.containers

    def get(self, name: str) -> FakeContainer:
        for c in self.containers:
            if c.name == name:
                return c
        raise Exception(f"container {name} not found")

    def create(self, image, name, **kwargs):  # noqa: ARG002
        labels = kwargs.get("labels", {})
        c = FakeContainer(name, labels=labels, status="created")
        self.containers.append(c)
        # mimic docker create returns container with start method
        return c


class FakeDocker:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self.containers = FakeDockerContainers(containers)
        self.images = FakeDockerImages()

def test_startup_cleanup_removes_orphans() -> None:
    orphan = FakeContainer(f"{CONTAINER_PREFIX}dead")
    unrelated = FakeContainer("some-other-container")
    manager = InstanceManager(
        Settings(mock_engine=False), docker_client=FakeDocker([orphan, unrelated])
    )

    import asyncio

    asyncio.run(manager.startup_cleanup())
    assert orphan.removed is True
    assert unrelated.removed is False


def test_stale_container_sweep_removes_untracked() -> None:
    stale = FakeContainer(f"{CONTAINER_PREFIX}ghost", labels={"inferna.managed": "true"})
    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([stale]))

    manager._remove_stale_containers_sync()
    assert stale.removed is True
