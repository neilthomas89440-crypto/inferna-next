# Multi-node parity verdict

One command reproduces this report, and every row records the environment it was measured in. A row
measured on a fake upstream or a mock stack is a **harness check**, never a verdict — the code
enforces that (`bench/bench/parity.py: verdict()`), because a 502 from a mock worker measures
nothing.

```bash
scripts/parity.sh --mode smoke --api http://localhost:9200 --api-key k --model fake-model \
                  --environment fake-upstream                       # harness check, any machine

scripts/parity.sh --mode single --admin-token "$JWT" --api-key "$KEY" --model Qwen/… --gpus 8 \
                  --environment hardware --baseline artifacts/parity-baseline.jsonl
scripts/parity.sh --mode multi  --admin-token "$JWT" --api-key "$KEY" --model Qwen/… \
                  --nodes 2 --gpus 4 --mode-flag pp \
                  --environment hardware --baseline artifacts/parity-baseline.jsonl
scripts/parity.sh --mode full   …        # 1×8 baseline, 2×4 pp, 2×4 tp, 4×2 pp
```

Exit codes: `0` measured and inside the budget, `1` a cell exceeded the error budget, `2`
prerequisites missing (no Docker/GPUs, deploy failed, ranks never became ready). CI and operators can
therefore tell “cannot measure here” from “the configuration is bad”.

Thresholds (roadmap §5): `R_tps ≥ 0.90`, `R_p95 ≤ 1.25` against the 1×8 baseline, plus TTFT p95
≤ 1.0 s, ITL p95 ≤ 50 ms, ≥ 20 tok/s, error rate < 0.1 %.

## Harness check (recorded 2026-10-14, this machine)

Command: `--mode smoke --scenario single --repeats 3 --warmup 1 --conc 4` against
`bench.fake_upstream` (deterministic SSE, 8 chunks × 10 ms).

| configuration | env | nodes×gpus | mode | TTFT p50 | TTFT p95 | ITL p95 | tok/s | errors | verdict |
|---|---|---|---|---|---|---|---|---|---|
| smoke | fake-upstream | 1×4 | n/a | 2.2 ms | 2.4 ms | 0.1 ms | — | 0/12 | harness-check |

Two things this row proves, and one it deliberately does **not**:

- the whole path works end to end: deploy-free measuring, the ratio math, the JSONL record
  (`artifacts/parity-smoke.jsonl`) and the table;
- throughput is reported as `—`, not as a number: the fixture delivers the stream in one read, so the
  generation window is sub-millisecond and any tokens/second computed from it would be an artefact
  (before the guard it printed 19 426 tok/s). This is the kind of number the roadmap warns about;
- it is **not** a verdict about multi-node anything.

## Hardware matrix — not yet measured

`BLOCKED: no GPU hosts in this environment.` The table below is the shape of the report; every cell
must be filled before stage 7 can claim a verdict, and an empty cell is a failure, not a pass.

| configuration | mode | netem | TTFT p50 | TTFT p95 | ITL p95 | tok/s | R_tps | R_p95 | verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1×8 (baseline) | single | none | ⬜ | ⬜ | ⬜ | ⬜ | — | — | ⬜ |
| 2×4 | pp | none | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 2×4 | pp | dc-2ms | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 2×4 | pp | wan-25ms-1% | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 2×4 | tp | dc-2ms | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 4×2 | pp | dc-2ms | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |

To fill it: bring up the topology (`docs/network-matrix.md`), apply the profile
(`sudo scripts/netem.sh apply dc-2ms`), run the row’s command, and append the JSONL to
`artifacts/`. Rows measured through the gateway get `--mode gateway` inside the harness; the
proxy-overhead delta comes from a second run with `--mode direct --direct-base`.

## Applicability boundary (what the design already implies)

From the M3 envelope numbers (`docs/network-matrix.md`) and the fact that pipeline parallelism is
latency-bound at these payloads:

- **pp is unusable above ≈ 150 ms RTT** at 0.5 MB per hand-off: the per-step cost is essentially
  `RTT + wire`, so 1 ms RTT gives ≈ 430 tok/s while 25 ms gives ≈ 20 tok/s. Between 25 ms and
  ~150 ms only throughput-oriented batch traffic is worth deploying.
- **tp must not be promised over a wide area.** The envelope table prints an upper bound that counts
  one collective per token; real engines run several per layer, so the interactive verdict there is
  optimistic by construction. The roadmap’s “no WAN tensor parallelism” stands until a hardware row
  says otherwise — and the table’s caveat exists so the number cannot be quoted as a promise.
- **Startup transfer is one-off**: ≈ 5 s per 16 GB at 25 Gbit/s, which is why every host keeps its
  own model volume (`docker-compose.multinode.yml`) instead of re-downloading weights per deploy.

## Raw data

Per-cell JSONL is written to `artifacts/parity-<timestamp>.jsonl` (git-ignored, like every other run
artefact) with the fields used by the table: configuration, mode, nodes, gpus, environment, TTFT
p50/p95, ITL p95, e2e p50, tokens/second, requests, errors, error_rate. The parity report is only as
good as those files: append them to the release notes when publishing a verdict.