"""Inferna Next benchmark harness.

Measures TTFT, inter-token latency and throughput of a deployed model, either through the
gateway (``--mode gateway``) or straight at an engine (``--mode direct``), and writes raw
per-request results as JSONL so runs can be compared or replayed.

Modules:

- ``client`` — streaming chat client (TTFT/ITL/TPS extraction)
- ``scenarios`` — deterministic load shapes (single sweep, agentic loop, long context, TTFT-only)
- ``metrics`` — percentile/throughput/histogram helpers
- ``report`` — markdown and JSON summaries
- ``trace`` — request recording and replay
- ``fake_upstream`` — deterministic [OI]-compatible upstream for harness self-checks
"""
