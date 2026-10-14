# Evidence — 06 M1.2 gateway telemetry (`w1/gateway-telemetry`)

What was built: per-model serving telemetry observed at the gateway — TTFT (first *content*
chunk), inter-token latency and per-request output throughput — plus two Grafana panels.

## Live end-to-end verification (not just unit tests)

Local stack: server on SQLite (gateway enabled) + mock worker + a **fake engine listening on the
instance port** (`bench.fake_upstream --port 8010 --chunks 6 --delay 0.02`), so the gateway
proxies to a streaming upstream exactly as it would to vLLM/SGLang.

```
gateway: 200 | bytes: 1019 | usage chunk: True
sample: data: {"id": "fake-1", "object": "chat.completion.chunk", "model": "fake-model",
               "choices": [{"index": 0, "delta": {"content": "token0 "}}]} …

inferna_ttft_seconds_count{engine="vllm",model="Qwen/Qwen2.5-0.5B-Instruct",parallelism="single"} 1.0
inferna_ttft_seconds_sum{engine="vllm",model="Qwen/Qwen2.5-0.5B-Instruct",parallelism="single"} 0.0416
inferna_inter_token_seconds_count{engine="vllm",model="Qwen/Qwen2.5-0.5B-Instruct",parallelism="single"} 5.0
inferna_inter_token_seconds_sum{engine="vllm",model="Qwen/Qwen2.5-0.5B-Instruct",parallelism="single"} 0.1495
inferna_output_tps_count{engine="vllm",model="Qwen/Qwen2.5-0.5B-Instruct",parallelism="single"} 1.0
inferna_output_tps_sum{engine="vllm",model="Qwen/Qwen2.5-0.5B-Instruct",parallelism="single"} 32.31
```

Reading of the numbers: 6 content chunks at 20 ms pacing → 5 inter-token intervals (mean ≈ 30 ms,
the extra ~10 ms is loopback + uvicorn overhead), TTFT 41.6 ms (the upstream opens with a
role-only chunk, which is *not* counted), throughput 32.3 tok/s ≈ 6 tokens / 185 ms. The labels
carry the engine from the instance and `parallelism="single"` (until M2.1 adds `parallel_mode`).

A side finding worth keeping: the first attempt returned `401 {"error":{"message":"unauthorized"}}`
— from the *fake engine*, not the gateway. The gateway forwards only `content-type` and `host`
(verified in `_proxy`), so a client's `inf-…` credential never reaches an engine. That is the
desired security property, and it is why the fixture grew a `--no-auth` mode (covered by a test).

## Gate matrix

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | 209 passed, 2 skipped (204 + 5 new in `test_gateway_telemetry.py`) |
| `cd bench && uv run ruff check . && uv run pyright && uv run pytest -q` | clean; `17 passed` (16 + the `--no-auth` case) |
| `deploy/grafana/dashboards/gateway.json` | parses; 10 panels (2 new: TTFT p50/p95, ITL p95 + mean tok/s) |
| proto / migrations / openapi | untouched by this slice |
| e2e | not applicable (no UI change); the live run above is the end-to-end proof |

New tests: `test_streaming_observes_ttft_itl_and_throughput` (one TTFT, one ITL, one TPS
observation; the role-only chunk must not count), `test_engine_label_follows_the_instance`
(sglang vs vllm label), `test_no_telemetry_when_the_upstream_never_streams_content`,
`test_disconnect_still_publishes_ttft` (a client hang-up keeps the measurement it earned),
`test_content_delta_parser_ignores_non_content_chunks`.

## Decision: no `X-Inferna-TTFT-Ms` response header

The plan listed that header; it is deliberately **not** implemented. The gateway streams, so at
the moment the response headers are flushed TTFT is unknown, and HTTP trailers are not honoured by
OpenAI clients. Emitting it for non-streaming requests only would require buffering the whole
upstream body in the gateway — a real behaviour change for a header nobody consumes. The metric is
the truthful channel; the plan's M1.2 bullet now says so.

The histogram is also the input the autoscaling policy needs (`hist_p95` in `bench/metrics.py`
computes the same quantile locally), so nothing downstream is blocked by skipping the header.
