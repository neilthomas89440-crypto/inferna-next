ENGINE_VENDORS: dict[str, frozenset[str]] = {
    "vllm": frozenset({"nvidia", "mock"}),
    "sglang": frozenset({"nvidia", "mock"}),
}
