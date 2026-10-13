"""Instance log snapshots: mock synthesis, real docker tails, non-blocking reads."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
# pyright: reportAttributeAccessIssue=false
import asyncio
import time

from inferna_worker.config import Settings
from inferna_worker.engines.manager import CONTAINER_PREFIX, InstanceManager
from inferna_worker.proto import cluster_pb2

MOCK_SETTINGS = Settings(mock_engine=True)


class _Container:
    def __init__(self, name: str, log_text: str = "fake log", delay: float = 0.0) -> None:
        self.name = name
        self._log_text = log_text
        self._delay = delay

    def logs(self, tail: int = 20) -> bytes:  # noqa: ARG002
        if self._delay:
            time.sleep(self._delay)
        return self._log_text.encode()


class _Containers:
    def __init__(self, containers: list[_Container]) -> None:
        self._containers = containers

    def get(self, name: str) -> _Container:
        for container in self._containers:
            if container.name == name:
                return container
        raise RuntimeError(f"container {name} not found")


class _Docker:
    def __init__(self, containers: list[_Container]) -> None:
        self.containers = _Containers(containers)


def _start_command(
    instance_id: str = "abc", requires_hf_token: bool = False
) -> cluster_pb2.InstanceCommand:
    return cluster_pb2.InstanceCommand(
        instance_id=instance_id,
        action="start",
        generation=1,
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


def _real_manager(containers: list[_Container]) -> InstanceManager:
    return InstanceManager(Settings(mock_engine=False), docker_client=_Docker(containers))


def _track_running(manager: InstanceManager, instance_id: str) -> None:
    manager._instances[instance_id] = {
        "state": "running",
        "port": 8010,
        "detail": "",
        "started_at": time.monotonic(),
        "generation": 1,
    }


async def test_mock_statuses_carry_synthesized_log_tail() -> None:
    manager = InstanceManager(MOCK_SETTINGS)
    await manager.reconcile([_start_command()])
    assert "mock: starting engine" in (await manager.statuses())[0].log_tail

    manager._instances["abc"]["started_at"] = time.monotonic() - 5
    statuses = await manager.statuses()
    assert statuses[0].state == "running"
    assert "mock: engine ready on port 8010" in statuses[0].log_tail


async def test_mock_error_state_reports_detail_in_log_tail() -> None:
    manager = InstanceManager(Settings(mock_engine=True, hf_token=""))
    await manager.reconcile([_start_command(requires_hf_token=True)])
    statuses = await manager.statuses()
    assert statuses[0].state == "error"
    assert "mock: error — model requires HF token" in statuses[0].log_tail


async def test_real_statuses_read_container_log_tail() -> None:
    container = _Container(f"{CONTAINER_PREFIX}abc", log_text="engine: boot ok\nengine: ready")
    manager = _real_manager([container])
    _track_running(manager, "abc")

    statuses = await manager.statuses()
    assert statuses[0].log_tail == "engine: boot ok\nengine: ready"
    await manager.shutdown()


async def test_log_tail_survives_missing_container() -> None:
    """A vanished container (or an empty read) must not wipe the last known snapshot."""
    manager = _real_manager([])
    _track_running(manager, "abc")
    manager._instances["abc"]["log_tail"] = "previous lines"

    statuses = await manager.statuses()
    assert statuses[0].log_tail == "previous lines"
    await manager.shutdown()


async def test_statuses_does_not_block_on_docker_logs() -> None:
    """`docker logs` sleeps off-loop, so the Sync loop stays responsive."""
    container = _Container(f"{CONTAINER_PREFIX}abc", log_text="slow", delay=0.4)
    manager = _real_manager([container])
    _track_running(manager, "abc")
    slow_containers = [
        _Container(f"{CONTAINER_PREFIX}slow{index}", log_text="slow", delay=0.4)
        for index in range(3)
    ]
    for index in range(3):
        manager._instances[f"slow{index}"] = {
            "state": "running",
            "port": 8011 + index,
            "detail": "",
            "started_at": time.monotonic(),
            "generation": 1,
        }
    slow_containers.append(container)
    manager._docker = _Docker(slow_containers)

    statuses = await asyncio.wait_for(manager.statuses(), timeout=2)
    assert len(statuses) == 4
    await manager.shutdown()
