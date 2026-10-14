# Benchmark harness and baselines

The harness lives in its own uv project (`bench/`) and measures what the roadmap’s thresholds
are expressed in: TTFT, inter-token latency, end-to-end latency and output throughput of a
deployed model — either through the gateway or straight at an engine.

```bash
bash scripts/bench.sh \
  --api http://localhost:8000 --api-key "$INFERNA_API_KEY" \
  --model Qwen/Qwen2.5-0.5B-Instruct \
  --scenario single --repeats 3 --warmup 1 \
  --environment hardware --out artifacts/bench-single.jsonl
```

| Flag | Meaning |
|---|---|
| `--scenario single\|agentic-loop\|long-context\|ttft-only` | load shape; `single` sweeps concurrency 1/4/8/16, `agentic-loop` resends a growing prefix 8 times, `long-context` uses 32k and 128k-token prompts, `ttft-only` caps output at one token |
| `--repeats`, `--warmup` | measured iterations per case and unmeasured warmup requests (both required — a run without warmup measures the harness, not the model) |
| `--conc` | concurrency (`0` keeps the scenario’s own sweep) |
| `--duration` | run each case for N seconds instead of a fixed repeat count |
| `--mode gateway\|direct` + `--direct-base` | measure through the proxy, or bypass it and talk to the engine origin directly |
| `--trace FILE` / `--replay FILE` | record the requests of a run, or replay a recorded trace with its original inter-arrival times |
| `--out FILE` | raw per-request JSONL (one object per request: status, TTFT, e2e, ITL list, tokens) |
| `--environment` | label written into the report (`mock`, `fake-upstream`, `hardware`, …) |

Exit code is `0` only when every request succeeded (`--allow-errors` relaxes that), so a run can
gate CI or a release check. The instrument uses `time.perf_counter()`: on Windows
`time.monotonic()` has ~15.6 ms granularity and reported sub-millisecond latencies as `0.0`.

## Baseline A — harness reproducibility (deterministic upstream)

A reproducibility check needs a server whose pacing is fixed, not a live model, so the harness
ships one: `bench/fake_upstream.py` serves `[OI]`-style SSE with a configurable chunk count and
delay (it is also what the unit tests run against).

```bash
uv run --project bench --directory bench python -m bench.fake_upstream --port 9200 --chunks 8 --delay 0.02
bash scripts/bench.sh --api http://127.0.0.1:9200 --api-key k --model fake-model \
  --scenario single --conc 4 --repeats 5 --warmup 2 --environment fake-upstream
```

Three consecutive runs (2026-10-14, Windows dev host, Python 3.12, 8 chunks × 20 ms):

| case | run 1 | run 2 | run 3 | spread |
|---|---|---|---|---|
| single conc=1 | 6.5 | 6.3 | 6.3 | 3.3% |
| single conc=4 | 6.1 | 6.2 | 6.3 | 4.2% |
| single conc=8 | 6.4 | 6.3 | 6.1 | 4.9% |
| single conc=16 | 6.8 | 6.6 | 6.6 | 3.7% |

**Worst p50 spread 4.9% — inside the ≤5% gate.** For reference, one full table from that run
(8 chunks, 20 ms apart → ITL ≈ 32 ms, e2e ≈ 247 ms, ~34 tok/s):

| case | requests | errors | TTFT p50 | TTFT p95 | e2e p50 | ITL p95 | mean tok/s |
|---|---|---|---|---|---|---|---|
| single conc=1 | 20 | 0 | 6.5 | 7.1 | 246.8 | 32.0 | 34.1 |
| single conc=4 | 20 | 0 | 6.0 | 6.9 | 246.6 | 32.1 | 34.5 |
| single conc=8 | 20 | 0 | 6.3 | 7.2 | 248.3 | 32.3 | 32.9 |
| single conc=16 | 20 | 0 | 6.7 | 7.8 | 255.8 | 33.7 | 32.0 |

## Baseline B — through the gateway on the mock stack

```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml up -d --build postgres server worker
# deploy Qwen/Qwen2.5-0.5B-Instruct in the UI, create an API key, then:
bash scripts/bench.sh --api http://localhost:8000 --api-key "$KEY" \
  --model Qwen/Qwen2.5-0.5B-Instruct --scenario ttft-only --repeats 3 --warmup 1 \
  --environment mock --allow-errors
```

Observed (2026-10-14, local uvicorn + mock worker):

| case | requests | errors | TTFT p50 | TTFT p95 | e2e p50 | ITL p95 | mean tok/s |
|---|---|---|---|---|---|---|---|
| ttft-only | 3 | 3 | — | — | — | — | — |

That is expected and worth stating plainly: the mock worker emulates the instance *lifecycle*
(no engine container, no HTTP server), so the gateway reaches a live instance, fails to connect
upstream and returns 502. The table prints and the run exits non-zero — which is the useful part
on this stack (routing, key handling and accounting are exercised); **latency numbers require a
real engine**.

## Baseline C — single-host hardware

`BLOCKED: no GPU host in this environment.` Record here, with `--environment hardware`:

- single-host DP baseline (1 instance, `--scenario single --repeats 5 --warmup 2`),
- the same model as 2 replicas (gateway load balancing), if the host has 2 GPUs,
- `--mode direct --direct-base http://<host>:<instance port>` for the proxy overhead delta
  (gateway run minus direct run, same scenario).

This table is the denominator for the parity coefficients (`R_tps`, `R_p95`) that stage 7 needs;
see `docs/roadmap/06-distributed-inference.md` §5 for the thresholds the numbers are compared
against (TTFT p95 ≤ 1.0 s cold / ≤ 0.6 s repeat, ITL ≤ 50 ms, ≥ 20 tok/s, errors < 0.1 %,
prefix-cache hit ≥ 80 %, `R_tps ≥ 0.90`, `R_p95 ≤ 1.25`).

## Traces

Recording a run and replaying it later is how a load shape gets reproduced across builds:

```bash
bash scripts/bench.sh … --scenario agentic-loop --trace artifacts/trace-agentic.jsonl --out artifacts/run.jsonl
bash scripts/bench.sh --api http://localhost:8000 --api-key "$KEY" --model <model> \
  --replay artifacts/trace-agentic.jsonl --environment hardware
```

Trace lines are `{"ts", "method", "path", "body"}`; replay preserves inter-arrival times (a
negative delta from clock skew is treated as zero). The autoscaling plan reuses this format for
its policy replay (`docs/roadmap/plans/plan-02-release-b-autoscaling.md`).