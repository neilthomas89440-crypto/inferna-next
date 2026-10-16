# Evidence — 06 M5 prefix-cache routing (`w2/cache-aware-routing`)

The gateway stops balancing blindly: a request whose prompt prefix a replica already holds goes to
that replica, because the engine (vLLM, SGLang) caches prompt prefixes automatically and a warm
prefill is the difference between a fast and a slow first token.

## What landed

| Piece | Behaviour |
|---|---|
| `services/prefix_cache.py` | rolling, chained block digests of the prompt (`prompt_text` flattens roles/content parts; blocks ≈128 tokens), and a per-instance ring of recently served prefixes bounded by TTL (600 s) and size (64 prefixes) |
| `overlap` | the **leading** shared run of blocks, not any intersection — a partly different prompt scores for its matching prefix only |
| `api/gateway._resolve_target` | candidates are scored by overlap; the best-overlap pool is then ordered by the existing rules (least active requests, then LRU). The rank-0 filter runs **before** scoring, so a follower can never win on a cache hit |
| `api/gateway._relay` | records the prompt blocks against the replica that served it (in `finally`, so a client that hangs up still warms the map — the engine did the prefill) |
| `services/metrics.py` | `inferna_prefix_cache_hit_ratio{model}` — cumulative matched/lookup ratio, outside `_ALL_GAUGES` |

Residency is deliberately an *inference*: it is what this gateway recently sent, not engine
introspection. A wrong guess costs one cold prefill; it can never route to a replica that cannot
serve the model, and a replica that stopped is simply not in the candidate set.

## Gates

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | 241 passed, 2 skipped (232 + 9 new; three balancing assertions were made prefix-neutral, see below) |
| `cd server && uv run python ../scripts/export-openapi.py --check` | untouched (no schema change) |
| M5 hardware gate (same replica ≥ 90 %, TTFT repeat ≤ 60 % of cold) | **BLOCKED: no GPU hosts** — needs the M4 parity run; the local proof is the routing test below |

New tests (`server/tests/test_prefix_cache.py`, 9): chained digests are deterministic and share
leading blocks for a shared prefix; `prompt_text` flattens roles and content parts; overlap counts
the leading run only; residency expires by TTL (with an injectable clock, because `time.monotonic()` resolution is ~15.6 ms on Windows) and is bounded by the ring; hit ratio is `None` until
something is looked up; **the replica holding the prefix wins even when both are idle**; the same
request falls back to plain balancing when that replica is down; and a follower with the resident
prefix is still skipped in favour of the head.

## Behaviour change this slice intentionally makes

Three assertions in `tests/test_gateway_least_loaded.py` used a *fixed* prompt for every request
(`"hi"`), so with prefix awareness they now measure the new policy rather than least-loaded/LRU.
Their helper sends a distinct prompt per call and says why in a docstring; the balancing contract
itself (rotate idle replicas, regain traffic after release, spread concurrent requests) is unchanged
and still asserted. Prefix stickiness has its own suite.

The cumulative hit ratio is asserted as `0.5` in the routing test on purpose: the first request had
nothing resident (miss), the second matched — the gauge is a fleet-wide rate, not "the last request
hit".

## Metrics to watch on hardware

- `inferna_prefix_cache_hit_ratio{model}` — should sit above 0.8 for agentic traffic (the M8 gate
  quotes the same number); a low value with a high request rate means the prefixes are not actually
  repeating (long conversations truncated by client-side history limits, or a client that rewrites
  the system prompt every turn).
- `inferna_ttft_seconds{model,engine,parallelism}` — the repeat-vs-cold comparison is what the M5
  gate measures; the histogram is already labelled as M1.2 requires.
- `inferna_instance_active_requests` — the tie-break input; if it stays flat while latency rises, the
  engine is queueing internally and the score has nothing to see (a known limitation of routing by
  active requests rather than by engine queue depth).