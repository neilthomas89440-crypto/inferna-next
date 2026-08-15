"""GPU detection parsing tests: rocm-smi JSON fixture, mock shape, MB conversion."""

from __future__ import annotations

import json

from inferna_worker.gpu import _to_mb, detect_mock, parse_amd_json

AMD_FIXTURE = {
    "card0": {
        "VRAM": {
            "used memory": 4338470912,
            "total memory": 17163091968,
        },
        "GPU ID": "0x744c",
        "Unique ID": "0x00000001",
        "Product Name": "AMD Radeon RX 6600",
        "Driver version": "6.5.2",
    },
    "card1": {
        "VRAM": {
            "used memory": 0,
            "total memory": 17163091968,
        },
        "GPU ID": "0x744c",
        "Unique ID": "0x00000002",
        "Product Name": "AMD Radeon RX 6600",
        "Driver version": "6.5.2",
    },
}


def test_parse_amd_json() -> None:
    gpus = parse_amd_json(json.dumps(AMD_FIXTURE))
    assert len(gpus) == 2
    first = gpus[0]
    assert first.index == 0
    assert first.vendor == "amd"
    assert first.name == "AMD Radeon RX 6600"
    assert first.vram_mb == 16368  # 17163091968 bytes // MiB
    assert first.used_vram_mb == 4137
    assert first.uuid == "0x00000001"
    assert first.driver_version == "6.5.2"
    assert gpus[1].used_vram_mb == 0
    # sorted by index
    assert [g.index for g in gpus] == [0, 1]


def test_parse_amd_json_empty() -> None:
    assert parse_amd_json("{}") == []


def test_parse_amd_json_missing_memory() -> None:
    raw = json.dumps({"card0": {"Product Name": "Radeon X"}})
    gpus = parse_amd_json(raw)
    assert gpus[0].vram_mb == 0
    assert gpus[0].name == "Radeon X"


def test_to_mb_variants() -> None:
    assert _to_mb(17163091968) == 16368  # bytes
    assert _to_mb("16368M") == 16368
    assert _to_mb("16G") == 16384
    assert _to_mb("0") == 0
    assert _to_mb("garbage") == 0


def test_detect_mock_shape_and_oscillation() -> None:
    gpus = detect_mock(tick=0)
    assert len(gpus) == 2
    assert [g.vendor for g in gpus] == ["mock", "mock"]
    assert [g.vram_mb for g in gpus] == [24576, 24576]
    assert [g.name for g in gpus] == ["Mock GPU A", "Mock GPU B"]
    assert gpus[0].utilization_pct == 0

    later = detect_mock(tick=1)
    # deterministic oscillation: values differ from tick 0
    assert (
        later[0].used_vram_mb != gpus[0].used_vram_mb
        or later[0].utilization_pct != gpus[0].utilization_pct
    )
    # values stay in valid ranges
    for gpu in later:
        assert 0 <= gpu.utilization_pct <= 100
        assert 0 <= gpu.used_vram_mb <= gpu.vram_mb
