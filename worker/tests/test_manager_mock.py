"""Mock-mode reconcile tests + real-mode orphan cleanup (fake docker client)."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportOptionalMemberAccess=false
import asyncio
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

async def test_startup_cleanup_removes_orphans() -> None:
    orphan = FakeContainer(f"{CONTAINER_PREFIX}dead")
    unrelated = FakeContainer("some-other-container")
    manager = InstanceManager(
        Settings(mock_engine=False), docker_client=FakeDocker([orphan, unrelated])
    )

    await manager.startup_cleanup()
    assert orphan.removed is True
    assert unrelated.removed is False
    await manager.shutdown()


def test_stale_container_sweep_removes_untracked() -> None:
    stale = FakeContainer(f"{CONTAINER_PREFIX}ghost", labels={"inferna.managed": "true"})
    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([stale]))

    manager._remove_stale_containers_sync()
    assert stale.removed is True


async def test_adopt_keeps_labeled_containers() -> None:
    from inferna_worker.engines.manager import LABEL_GENERATION, LABEL_MANAGED, LABEL_PORT

    labeled = FakeContainer(
        f"{CONTAINER_PREFIX}keep",
        labels={LABEL_MANAGED: "true", LABEL_GENERATION: "5", LABEL_PORT: "8010"},
        status="running",
    )
    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([labeled]))
    await manager.adopt_existing()
    assert "keep" in manager._instances
    info = manager._instances["keep"]
    assert info["state"] == "running"
    assert info["port"] == 8010
    assert info["generation"] == 5
    assert labeled.removed is False
    await manager.shutdown()


async def test_adopt_removes_unlabeled_legacy() -> None:
    legacy = FakeContainer(f"{CONTAINER_PREFIX}legacy")
    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([legacy]))
    await manager.adopt_existing()
    assert legacy.removed is True
    assert "legacy" not in manager._instances
    await manager.shutdown()

def test_start_skipped_when_generation_applied() -> None:
    from inferna_worker.engines.manager import LABEL_GENERATION, LABEL_MANAGED, LABEL_PORT

    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([]))
    manager._instances["skip"] = {
        "state": "running",
        "generation": 2,
        "port": 8010,
        "detail": "",
        "started_at": 0,
    }
    fake = FakeContainer(
        f"{CONTAINER_PREFIX}skip", labels={LABEL_MANAGED: "true", LABEL_GENERATION: "2", LABEL_PORT: "8010"}, status="running"
    )
    manager._docker.containers.containers.append(fake)
    cmd = _start_command(instance_id="skip", generation=1)
    manager._start_sync(cmd)
    assert manager._instances["skip"]["generation"] == 2
    assert fake.removed is False
    cmd2 = _start_command(instance_id="skip", generation=2)
    manager._start_sync(cmd2)
    assert fake.removed is False


def test_restart_higher_generation_recreates() -> None:
    from inferna_worker.engines.manager import LABEL_GENERATION, LABEL_MANAGED, LABEL_PORT

    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([]))
    old = FakeContainer(
        f"{CONTAINER_PREFIX}rst", labels={LABEL_MANAGED: "true", LABEL_GENERATION: "1", LABEL_PORT: "8010"}, status="running"
    )
    manager._docker.containers.containers.append(old)
    manager._instances["rst"] = {"state": "running", "generation": 1, "port": 8010, "detail": "", "started_at": 0}
    cmd = _start_command(instance_id="rst", generation=2)
    manager._start_sync(cmd)
    assert old.removed is True
    assert manager._instances["rst"]["generation"] == 2
    new = [c for c in manager._docker.containers.containers if c.name == f"{CONTAINER_PREFIX}rst" and not c.removed]
    assert len(new) == 1
    assert new[0].labels[LABEL_GENERATION] == "2"
async def test_error_path_records_generation() -> None:
    manager = InstanceManager(Settings(mock_engine=True, hf_token=""))
    cmd = _start_command(instance_id="err", requires_hf_token=True, generation=7)
    await manager.reconcile([cmd])
    assert "err" in manager._instances
    assert manager._instances["err"]["generation"] == 7
    assert manager._instances["err"]["state"] == "error"
    statuses = manager.statuses()
    m = {s.instance_id: s for s in statuses}
    assert m["err"].generation == 7
    await manager.shutdown()
async def test_reconcile_coalesces_into_pending() -> None:
    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([]))
    orig = manager._start_sync

    def slow(cmd):
        time.sleep(0.4)
        orig(cmd)

    manager._start_sync = slow  # type: ignore[method-assign]
    cmd1 = _start_command(instance_id="coal", generation=1)
    await manager.reconcile([cmd1])
    assert manager._batch_task is not None and not manager._batch_task.done()
    cmd2 = _start_command(instance_id="coal", generation=2)
    await manager.reconcile([cmd2])
    assert manager._pending["coal"].generation == 2
    await asyncio.sleep(1.2)
    if manager._batch_task is not None:
        try:
            await asyncio.wait_for(manager._batch_task, timeout=2)
        except asyncio.TimeoutError:
            pass
    assert manager._instances["coal"]["generation"] == 2
    await manager.shutdown()


async def test_restart_during_inflight_batch_recreates_once() -> None:
    manager = InstanceManager(Settings(mock_engine=False), docker_client=FakeDocker([]))
    orig = manager._start_sync

    def slow2(cmd):
        time.sleep(0.3)
        orig(cmd)

    manager._start_sync = slow2  # type: ignore[method-assign]
    cmd1 = _start_command(instance_id="batch", generation=1)
    await manager.reconcile([cmd1])
    await asyncio.sleep(0.05)
    cmd2 = _start_command(instance_id="batch", generation=2)
    await manager.reconcile([cmd2])
    await asyncio.sleep(1.2)
    if manager._batch_task is not None:
        try:
            await asyncio.wait_for(manager._batch_task, timeout=2)
        except asyncio.TimeoutError:
            pass
    assert manager._instances["batch"]["generation"] == 2
    containers = [c for c in manager._docker.containers.containers if c.name == f"{CONTAINER_PREFIX}batch" and not c.removed]
    assert len(containers) == 1
    await manager.shutdown()

