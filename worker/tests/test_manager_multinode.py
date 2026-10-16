"""Worker side of multi-node: engine flags, environment, host networking, teardown order."""

from __future__ import annotations

import asyncio
import time

from inferna_worker.config import Settings
from inferna_worker.engines.base import (
    CONTAINER_PORT,
    MultiNodeSpec,
    build_command,
    multi_node_env,
)
from inferna_worker.engines.manager import CONTAINER_PREFIX, InstanceManager
from inferna_worker.proto import cluster_pb2

VLLM_OLD_COMMAND = [
    "vllm",
    "serve",
    "Qwen/Qwen2.5-0.5B-Instruct",
    "--port",
    str(CONTAINER_PORT),
    "--gpu-memory-utilization",
    "0.9",
    "--tensor-parallel-size",
    "1",
    "--max-num-seqs",
    "32",
]


class _Container:
    def __init__(self, name: str, status: str = "running") -> None:
        self.name = name
        self.status = status
        self.stopped = False
        self.removed = False

    def start(self) -> None:
        self.status = "running"

    def stop(self, timeout: int = 10) -> None:  # noqa: ARG002
        self.stopped = True
        self.status = "exited"

    def remove(self, force: bool = False) -> None:  # noqa: ARG002
        self.removed = True

    def logs(self, tail: int = 20) -> bytes:  # noqa: ARG002
        return b"follower log line"


class _Containers:
    def __init__(self, containers: dict[str, _Container], events: list[str]) -> None:
        self._containers = containers
        self.events = events

    def get(self, name: str) -> _Container:
        if name not in self._containers:
            raise RuntimeError(f"container {name} not found")
        return self._containers[name]

    def list(self, all: bool = False) -> list[_Container]:  # noqa: ARG002
        return list(self._containers.values())

    def create(self, image: str, **kwargs) -> _Container:  # noqa: ARG002
        container = _Container(kwargs["name"])
        self._containers[kwargs["name"]] = container
        return container


class _Images:
    def __init__(self) -> None:
        self.pulled: list[str] = []

    def pull(self, image: str) -> None:
        self.pulled.append(image)


class _Docker:
    def __init__(
        self, containers: dict[str, _Container] | None = None, events: list[str] | None = None
    ) -> None:
        self.events = events if events is not None else []
        self.containers = _Containers(containers or {}, self.events)
        self.images = _Images()


def _spec(mode: str = "pp", *, world: int = 2, rank: int = 0) -> MultiNodeSpec:
    return MultiNodeSpec(
        mode=mode,
        world_size=world,
        node_rank=rank,
        master_host="10.0.0.1",
        master_port=8110,
    )


def _config(engine: str = "vllm", gpus: int = 1, **overrides) -> cluster_pb2.EngineConfig:
    payload = {
        "engine": engine,
        "model_name": "Qwen/Qwen2.5-0.5B-Instruct",
        "profile": "latency",
        "gpu_indexes": list(range(gpus)),
        "vram_required_mb": 2048,
        "port": 8010,
        "requires_hf_token": False,
    }
    payload.update(overrides)
    return cluster_pb2.EngineConfig(**payload)


def _start_command(
    instance_id: str,
    engine: str = "vllm",
    *,
    gpus: int = 1,
    mode: str = "pp",
    world: int = 2,
    rank: int = 0,
) -> cluster_pb2.InstanceCommand:
    config = _config(engine, gpus)
    config.parallelism.CopyFrom(
        cluster_pb2.Parallelism(mode=mode, tensor_parallel=gpus, world_size=world)
    )
    config.rendezvous.CopyFrom(
        cluster_pb2.Rendezvous(
            master_address="10.0.0.1",
            master_port=8110,
            node_rank=rank,
            num_nodes=world,
            env=["MASTER_ADDR=10.0.0.1", "MASTER_PORT=8110"],
        )
    )
    return cluster_pb2.InstanceCommand(
        instance_id=instance_id, action="start", generation=1, config=config
    )


# --- command lines ---


def test_single_node_command_is_byte_identical() -> None:
    """Invariant §3.6: without a spec the command must not change at all."""
    assert build_command(_config()) == VLLM_OLD_COMMAND


def test_vllm_pp_rank0_binds_the_host_port_and_joins_the_rendezvous() -> None:
    command = build_command(_config(), _spec("pp"), serve_port=8010)

    assert command[:4] == ["vllm", "serve", "Qwen/Qwen2.5-0.5B-Instruct", "--port"]
    assert command[4] == "8010"  # host networking: the engine binds the host port
    assert "--pipeline-parallel-size" in command
    assert command[command.index("--pipeline-parallel-size") + 1] == "2"
    assert command[command.index("--nnodes") + 1] == "2"
    assert command[command.index("--node-rank") + 1] == "0"
    assert command[command.index("--master-addr") + 1] == "10.0.0.1"
    assert command[command.index("--master-port") + 1] == "8110"


def test_vllm_pp_follower_drops_the_port() -> None:
    command = build_command(_config(), _spec("pp", rank=1), serve_port=8010)

    assert "--port" not in command  # only the head serves the API
    assert command[command.index("--node-rank") + 1] == "1"


def test_vllm_tp_uses_one_global_tensor_group() -> None:
    command = build_command(_config(gpus=2), _spec("tp", world=2), serve_port=8010)

    assert command[command.index("--tensor-parallel-size") + 1] == "4"  # 2 GPUs × 2 hosts
    assert command[command.index("--distributed-executor-backend") + 1] == "mp"
    assert "--pipeline-parallel-size" not in command
    assert command[command.index("--nnodes") + 1] == "2"


def test_sglang_pp_uses_dist_init_addr() -> None:
    command = build_command(_config("sglang"), _spec("pp", rank=1), serve_port=8010)

    assert command[command.index("--dist-init-addr") + 1] == "10.0.0.1:8110"
    assert command[command.index("--nnodes") + 1] == "2"
    assert command[command.index("--node-rank") + 1] == "1"
    assert "--port" not in command


def test_multi_node_env_only_adds_what_the_worker_owns() -> None:
    env = multi_node_env(_spec(), "gpu-host-1", "ens1f0")

    assert env == {"NCCL_SOCKET_IFNAME": "ens1f0", "VLLM_HOST_IP": "gpu-host-1"}
    assert multi_node_env(None, "gpu-host-1", "ens1f0") == {}


def test_spec_is_absent_for_single_node_configs() -> None:
    assert MultiNodeSpec.from_config(_config()) is None

    config = _config()
    config.parallelism.CopyFrom(cluster_pb2.Parallelism(mode="single", world_size=1))
    assert MultiNodeSpec.from_config(config) is None


# --- container wiring ---


async def test_group_start_uses_host_networking_and_injects_rendezvous_env() -> None:
    docker = _Docker()
    settings = Settings(mock_engine=False, worker_hostname="gpu-host-1", netif="ens1f0")
    manager = InstanceManager(settings, docker_client=docker)
    created: dict[str, object] = {}

    def _create(image: str, **kwargs):
        created.update(kwargs)
        container = _Container(kwargs["name"])
        docker.containers._containers[kwargs["name"]] = container
        return container

    docker.containers.create = _create  # type: ignore[method-assign]

    await asyncio.to_thread(manager._start_sync, _start_command("abc", rank=0))

    assert created["network_mode"] == "host"
    assert created["ports"] is None
    environment = created["environment"]
    assert isinstance(environment, dict)
    assert environment["MASTER_ADDR"] == "10.0.0.1"
    assert environment["MASTER_PORT"] == "8110"
    assert environment["NCCL_SOCKET_IFNAME"] == "ens1f0"
    assert environment["VLLM_HOST_IP"] == "gpu-host-1"
    assert manager._instances["abc"]["node_rank"] == 0

    await manager.shutdown()


async def test_single_node_start_keeps_port_mapping() -> None:
    docker = _Docker()
    manager = InstanceManager(Settings(mock_engine=False), docker_client=docker)
    created: dict[str, object] = {}

    def _create(image: str, **kwargs):
        created.update(kwargs)
        container = _Container(kwargs["name"])
        docker.containers._containers[kwargs["name"]] = container
        return container

    docker.containers.create = _create  # type: ignore[method-assign]

    command = cluster_pb2.InstanceCommand(
        instance_id="solo", action="start", generation=1, config=_config()
    )
    await asyncio.to_thread(manager._start_sync, command)

    assert created["network_mode"] is None
    assert created["ports"] == {"8000/tcp": 8010}
    assert created["command"] == VLLM_OLD_COMMAND

    await manager.shutdown()


async def test_follower_becomes_running_when_its_container_stays_up() -> None:
    name = f"{CONTAINER_PREFIX}follower"
    docker = _Docker({name: _Container(name, status="running")})
    manager = InstanceManager(
        Settings(mock_engine=False, mn_rank_ready_seconds=0.05), docker_client=docker
    )
    manager._instances["follower"] = {
        "state": "starting",
        "port": 0,
        "detail": "",
        "started_at": time.monotonic(),
        "generation": 1,
        "node_rank": 1,
    }

    await asyncio.wait_for(manager._follower_probe("follower"), timeout=5)

    assert manager._instances["follower"]["state"] == "running"
    await manager.shutdown()


async def test_follower_probe_reports_a_dead_container() -> None:
    name = f"{CONTAINER_PREFIX}follower"
    docker = _Docker({name: _Container(name, status="exited")})
    manager = InstanceManager(
        Settings(mock_engine=False, mn_rank_ready_seconds=0.05), docker_client=docker
    )
    manager._instances["follower"] = {
        "state": "starting",
        "port": 0,
        "detail": "",
        "started_at": time.monotonic(),
        "generation": 1,
        "node_rank": 1,
    }

    await asyncio.wait_for(manager._follower_probe("follower"), timeout=5)

    assert manager._instances["follower"]["state"] == "error"
    assert manager._instances["follower"]["detail"]  # the log tail, or "container exited"
    await manager.shutdown()


async def test_followers_stop_before_the_head() -> None:
    """Teardown order: a dead head strands its followers on a rendezvous point that is gone."""
    manager = InstanceManager(Settings(mock_engine=True))
    order: list[str] = []

    def _stop(instance_id: str) -> None:
        order.append(instance_id)
        manager._instances[instance_id]["state"] = "stopped"

    manager._stop_sync = _stop  # type: ignore[method-assign]
    for instance_id, rank in (("head-1", 0), ("follower-1", 1), ("follower-2", 2)):
        manager._instances[instance_id] = {
            "state": "running",
            "port": 8010 if rank == 0 else 0,
            "detail": "",
            "started_at": time.monotonic(),
            "generation": 1,
            "node_rank": rank,
        }

    await manager._apply_batch(
        [
            cluster_pb2.InstanceCommand(instance_id="head-1", action="stop", generation=1),
            cluster_pb2.InstanceCommand(instance_id="follower-1", action="stop", generation=1),
            cluster_pb2.InstanceCommand(instance_id="follower-2", action="stop", generation=1),
        ]
    )

    assert order == ["follower-2", "follower-1", "head-1"]
    await manager.shutdown()


async def test_portless_instance_schedules_no_probe() -> None:
    """A port-less instance has no API to poll; probing `127.0.0.1:0` used to wedge the loop.

    On Windows the proactor event loop can sit in `finish_connect` for port 0 instead of
    failing fast, which stalls anything awaiting the manager.
    """
    manager = InstanceManager(Settings(mock_engine=False), docker_client=_Docker())
    # Not a follower (no rank): the port check is what must stop the probe.
    manager._instances["portless"] = {
        "state": "starting",
        "port": 0,
        "detail": "",
        "started_at": time.monotonic(),
        "generation": 1,
        "node_rank": None,
    }
    manager._pending_probes.append(("portless", 0))

    manager._schedule_pending_probes()

    assert "portless" not in manager._probe_tasks
    await manager.shutdown()
