"""Engine command builders: vLLM / SGLang flag tables for latency/throughput profiles.

Multi-node commands are built from the contract's `Parallelism`/`Rendezvous` messages: the
server owns the topology and the worker only translates it into engine flags — it never
decides ranks (roadmap §3.4).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from inferna_worker.config import Settings
from inferna_worker.proto import cluster_pb2

CONTAINER_PORT = 8000

VLLM_BASE = [
    "vllm",
    "serve",
    "{model}",
    "--port",
    str(CONTAINER_PORT),
    "--gpu-memory-utilization",
    "0.9",
    "--tensor-parallel-size",
    "{tp}",
]

SGLANG_BASE = [
    "python3",
    "-m",
    "sglang.launch_server",
    "--model-path",
    "{model}",
    "--port",
    str(CONTAINER_PORT),
    "--tp",
    "{tp}",
    "--mem-fraction-static",
    "0.85",
]

# Profile tweaks appended to the base command.
PROFILE_FLAGS: dict[str, dict[str, dict[str, str]]] = {
    "vllm": {
        "latency": {"--max-num-seqs": "32"},
        "throughput": {"--max-num-seqs": "256"},
    },
    "sglang": {
        "latency": {"--max-running-requests": "32"},
        "throughput": {"--max-running-requests": "256"},
    },
}


@dataclass(slots=True)
class MultiNodeSpec:
    """Topology of one rank, translated from the server's Parallelism/Rendezvous."""

    mode: str  # "pp" | "tp" | "dp" | "pd"
    world_size: int
    node_rank: int
    master_host: str
    master_port: int

    @classmethod
    def from_config(cls, config: cluster_pb2.EngineConfig) -> MultiNodeSpec | None:
        """None when the config is single-node (absent/empty messages = the old contract)."""
        if not config.HasField("parallelism") or config.parallelism.mode in ("", "single"):
            return None
        return cls(
            mode=config.parallelism.mode,
            world_size=max(1, config.parallelism.world_size),
            node_rank=config.rendezvous.node_rank,
            master_host=config.rendezvous.master_address,
            master_port=config.rendezvous.master_port,
        )


def multi_node_env(
    spec: MultiNodeSpec | None, advertise_host: str, netif: str
) -> dict[str, str]:
    """Runtime environment a rank needs *besides* the server's `Rendezvous.env`.

    Only the worker knows its NICs, so `NCCL_SOCKET_IFNAME` and `VLLM_HOST_IP` are decided
    here; `MASTER_ADDR`/`MASTER_PORT` arrive from the server.
    """
    if spec is None:
        return {}
    env: dict[str, str] = {"NCCL_SOCKET_IFNAME": netif}
    if advertise_host:
        env["VLLM_HOST_IP"] = advertise_host
    return env


def _multinode_flags(
    engine: str, spec: MultiNodeSpec, tensor_parallel: int
) -> list[str]:
    """Engine flags that connect one rank to the rendezvous point."""
    world = max(1, spec.world_size)
    if engine == "vllm":
        if spec.mode == "tp":
            # Data/tensor sharding inside the host plus across hosts: one global TP group.
            # The base command already carries `--tensor-parallel-size`, so replace its value
            # instead of appending a second copy of the flag.
            flags = ["--distributed-executor-backend", "mp"]
        else:
            # pp (and pd for now): pipeline stages span the ranks.
            flags = ["--pipeline-parallel-size", str(world)]
        flags += [
            "--nnodes",
            str(world),
            "--node-rank",
            str(spec.node_rank),
            "--master-addr",
            spec.master_host,
            "--master-port",
            str(spec.master_port),
        ]
        return flags
    if engine == "sglang":
        # The base command already carries `--tp`; a tp group replaces its value below, and a
        # pp group spans ranks instead.
        flags = [] if spec.mode == "tp" else ["--pipeline-parallel-size", str(world)]
        flags += [
            "--nnodes",
            str(world),
            "--node-rank",
            str(spec.node_rank),
            "--dist-init-addr",
            f"{spec.master_host}:{spec.master_port}",
        ]
        return flags
    return []


def build_command(
    config: cluster_pb2.EngineConfig,
    spec: MultiNodeSpec | None = None,
    serve_port: int = CONTAINER_PORT,
) -> list[str]:
    """Container command for an engine config (shlex-safe list form).

    With `spec=None` the command is byte-identical to the single-node contract. Multi-node
    commands replace `--port` with the head's host port (the container runs with host
    networking) and append the engine's rendezvous flags; a follower drops `--port`
    entirely — only the head serves the API.
    """
    tensor_parallel = max(1, len(config.gpu_indexes))
    if config.engine == "vllm":
        base = VLLM_BASE
    elif config.engine == "sglang":
        base = SGLANG_BASE
    else:
        raise ValueError(f"unknown engine: {config.engine}")

    command = [part.format(model=config.model_name, tp=tensor_parallel) for part in base]
    profile_flags = PROFILE_FLAGS.get(config.engine, {}).get(config.profile, {})
    for flag, value in profile_flags.items():
        command.extend([flag, value])

    if spec is not None:
        port_index = command.index("--port")
        if spec.node_rank > 0:
            del command[port_index : port_index + 2]
        else:
            command[port_index + 1] = str(serve_port)
        if spec.mode == "tp":
            # One TP group across hosts: widen the base flag rather than duplicating it.
            tp_flag = "--tensor-parallel-size" if config.engine == "vllm" else "--tp"
            tp_index = command.index(tp_flag)
            command[tp_index + 1] = str(tensor_parallel * max(1, spec.world_size))
        command += _multinode_flags(config.engine, spec, tensor_parallel)
    # `serve_port` only matters with host networking (multi-node): a single-node container keeps
    # listening on the container port, and docker maps it to the allocated host port.
    return command


def build_command_shlex(config: cluster_pb2.EngineConfig) -> str:
    """Same command as a shell string (used directly with `command=` where allowed)."""
    return shlex.join(build_command(config))


def image_for(engine: str, settings: Settings) -> str:
    if engine == "vllm":
        return settings.vllm_image
    if engine == "sglang":
        return settings.sglang_image
    raise ValueError(f"unknown engine: {engine}")
