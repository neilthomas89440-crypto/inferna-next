"""Engine command builders: vLLM / SGLang flag tables for latency/throughput profiles."""

from __future__ import annotations

import shlex

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


def build_command(config: cluster_pb2.EngineConfig) -> list[str]:
    """Container command for an engine config (shlex-safe list form)."""
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
