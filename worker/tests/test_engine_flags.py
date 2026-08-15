"""Engine flag-table tests: vLLM / SGLang base commands + profile flags."""

from __future__ import annotations

import pytest

from inferna_worker.config import Settings
from inferna_worker.engines.base import build_command, image_for
from inferna_worker.proto import cluster_pb2


def _config(
    engine: str, profile: str, gpu_indexes: list[int] | None = None
) -> cluster_pb2.EngineConfig:
    return cluster_pb2.EngineConfig(
        engine=engine,
        model_name="Qwen/Qwen2.5-7B-Instruct",
        profile=profile,
        gpu_indexes=gpu_indexes or [0, 1],
        vram_required_mb=16384,
        port=8010,
    )


def test_vllm_base_command() -> None:
    command = build_command(_config("vllm", "latency"))
    assert command[0] == "vllm"
    assert command[1] == "serve"
    assert "Qwen/Qwen2.5-7B-Instruct" in command
    assert "--port" in command and "8000" in command
    assert "--gpu-memory-utilization" in command and "0.9" in command
    assert "--tensor-parallel-size" in command
    assert command[command.index("--tensor-parallel-size") + 1] == "2"  # 2 GPUs


def test_vllm_latency_vs_throughput() -> None:
    latency = build_command(_config("vllm", "latency"))
    throughput = build_command(_config("vllm", "throughput"))
    assert "--max-num-seqs" in latency
    assert latency[latency.index("--max-num-seqs") + 1] == "32"
    assert throughput[throughput.index("--max-num-seqs") + 1] == "256"


def test_sglang_base_command() -> None:
    command = build_command(_config("sglang", "throughput"))
    assert command[0] == "python3"
    assert command[1] == "-m"
    assert command[2] == "sglang.launch_server"
    assert "--model-path" in command
    assert "--tp" in command
    assert command[command.index("--tp") + 1] == "2"
    assert "--mem-fraction-static" in command and "0.85" in command


def test_sglang_latency_vs_throughput() -> None:
    latency = build_command(_config("sglang", "latency"))
    throughput = build_command(_config("sglang", "throughput"))
    assert latency[latency.index("--max-running-requests") + 1] == "32"
    assert throughput[throughput.index("--max-running-requests") + 1] == "256"


def test_single_gpu_tensor_parallel_one() -> None:
    command = build_command(_config("vllm", "latency", gpu_indexes=[0]))
    assert command[command.index("--tensor-parallel-size") + 1] == "1"


def test_unknown_engine_rejected() -> None:
    with pytest.raises(ValueError):
        build_command(_config("llama.cpp", "latency"))


def test_image_for() -> None:
    settings = Settings(
        vllm_image="vllm/vllm-openai:v0.8.5", sglang_image="lmsysorg/sglang:v0.4.6.post1"
    )
    assert image_for("vllm", settings) == "vllm/vllm-openai:v0.8.5"
    assert image_for("sglang", settings) == "lmsysorg/sglang:v0.4.6.post1"
    with pytest.raises(ValueError):
        image_for("other", settings)
