"""GPU detection: NVIDIA (NVML), AMD (rocm-smi), mock (demo)."""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

MOCK_VRAM_MB = 24576


@dataclass
class GPUInfo:
    index: int
    vendor: str
    name: str
    vram_mb: int
    used_vram_mb: int
    utilization_pct: int
    uuid: str = ""
    driver_version: str = ""


def _to_mb(value) -> int:
    """Normalize rocm-smi memory values (bytes int or suffixed string) to MB."""
    if isinstance(value, (int, float)):
        return int(value) // (1024 * 1024)
    text = str(value).strip()
    digits = ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            digits += ch
        else:
            break
    try:
        number = float(digits)
    except ValueError:
        return 0
    unit = text[len(digits) :].strip().upper()
    if unit in ("", "B"):
        return int(number // (1024 * 1024))
    if unit == "K":
        return int(number // 1024)
    if unit == "M":
        return int(number)
    if unit == "G":
        return int(number * 1024)
    if unit == "T":
        return int(number * 1024 * 1024)
    return int(number)


def detect_nvidia() -> list[GPUInfo]:
    """NVML-based NVIDIA detection; raises if unavailable or no devices."""
    import pynvml  # nvidia-ml-py

    pynvml.nvmlInit()
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            raise RuntimeError("no NVIDIA devices")

        def _to_str(value) -> str:
            return value.decode() if isinstance(value, bytes) else str(value)

        driver_version = _to_str(pynvml.nvmlSystemGetDriverVersion())
        gpus: list[GPUInfo] = []
        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            name = _to_str(pynvml.nvmlDeviceGetName(handle))
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            gpus.append(
                GPUInfo(
                    index=index,
                    vendor="nvidia",
                    name=name,
                    vram_mb=int(memory.total) // (1024 * 1024),
                    used_vram_mb=int(memory.used) // (1024 * 1024),
                    utilization_pct=int(utilization.gpu),
                    uuid=_to_str(pynvml.nvmlDeviceGetUUID(handle)),
                    driver_version=driver_version,
                )
            )
        return gpus
    finally:
        pynvml.nvmlShutdown()


def parse_amd_json(raw: str) -> list[GPUInfo]:
    """Parse `rocm-smi --showmeminfo vram --showproductname --json` output."""
    data = json.loads(raw)
    gpus: list[GPUInfo] = []
    for card_key, info in data.items():
        if not str(card_key).startswith("card"):
            continue
        index = int(str(card_key)[4:])
        vram = info.get("VRAM", {}) or {}
        gpus.append(
            GPUInfo(
                index=index,
                vendor="amd",
                name=str(info.get("Product Name") or info.get("GPU ID") or f"AMD GPU {index}"),
                vram_mb=_to_mb(vram.get("total memory", 0)),
                used_vram_mb=_to_mb(vram.get("used memory", 0)),
                utilization_pct=0,
                uuid=str(info.get("Unique ID", "")),
                driver_version=str(info.get("Driver version", "unknown")),
            )
        )
    return sorted(gpus, key=lambda g: g.index)


def detect_amd() -> list[GPUInfo]:
    """ROCm detection via the rocm-smi CLI; raises if unavailable."""
    if platform.system() == "Windows":
        raise RuntimeError("rocm-smi is Linux-only")
    result = subprocess.run(
        ["rocm-smi", "--showmeminfo", "vram", "--showproductname", "--json"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    gpus = parse_amd_json(result.stdout)
    if not gpus:
        raise RuntimeError("no AMD devices reported")
    return gpus


def detect_mock(tick: int = 0) -> list[GPUInfo]:
    """Synthesize two GPUs with deterministic oscillating usage so dashboards move."""
    gpus: list[GPUInfo] = []
    for index in range(2):
        used = ((tick * 3 + index * 5) % (MOCK_VRAM_MB // 4)) + 1024
        utilization = (tick * 17 + index * 23) % 100
        gpus.append(
            GPUInfo(
                index=index,
                vendor="mock",
                name=f"Mock GPU {'A' if index == 0 else 'B'}",
                vram_mb=MOCK_VRAM_MB,
                used_vram_mb=used,
                utilization_pct=utilization,
                uuid=f"mock-gpu-{index}",
                driver_version="mock-1.0",
            )
        )
    return gpus


def detect(mock: bool = False, tick: int = 0) -> list[GPUInfo]:
    """Try NVIDIA, then AMD, then fall back to an empty list (no GPUs)."""
    if mock:
        return detect_mock(tick)
    try:
        return detect_nvidia()
    except Exception:  # noqa: BLE001
        logger.info("NVIDIA detection unavailable; trying ROCm")
    try:
        return detect_amd()
    except Exception:  # noqa: BLE001
        logger.info("AMD detection unavailable; no GPUs reported")
    return []
