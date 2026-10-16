# Coding validation

Answers the question the latency numbers cannot: **is this configuration usable for coding help?**
An agent loop needs a first token in well under a second and a steady token rate, and the answers have
to be *correct*, not just fast.

```bash
scripts/coding-validation.sh --api http://server:8000 --api-key "$KEY" \
    --model Qwen/Qwen2.5-Coder-7B-Instruct --environment hardware --out artifacts/coding-$(date +%s).jsonl
```

`--smoke` asserts only that every task was *scored* (no request-level failure) whatever the answers
were — that is what the CI job runs against the deterministic fixture. Without it, exit codes are
`0` all passed, `1` failures on hardware, `2` failures on a non-hardware stack.

## The task set (`coding/tasks.json`)

12 deterministic tasks, 10 text-checked and 2 opt-in `pytest` runs:

| group | tasks | how the answer is checked |
|---|---|---|
| generate code | `gen-func-body`, `fix-import-order`, `sql-select`, `refactor-loop` | required fragments / regex, code fences forbidden |
| fix and explain | `fix-off-by-one`, `grep-error-cause`, `unit-test-name`, `commit-message` | a specific label, a name matching `test_…register…hostname…`, a conventional-commit subject |
| structured output | `json-schema-answer` | parsed JSON with the right types and values (prose around the object is tolerated) |
| operator judgement | `explain-metric`, `fix-fastapi` variants | two sentences mentioning the concept |
| **opt-in execution** | `pytest-fix-fastapi`, `pytest-fix-off-by-one` | the answer is written to a temp dir and pytest runs there with a 60 s timeout (`--allow-code-execution`) |

Two deliberate choices:

- **Executing model output is opt-in.** The `pytest` tasks are skipped unless
  `--allow-code-execution` is passed, so CI never runs generated code by default; when it is passed,
  the run happens in a fresh temporary directory with a timeout.
- **The answer that is scored is the answer that was measured.** The streaming client returns the
  collected text, so there is no “measured A, graded B” path (an earlier draft asked twice).

## Harness check (recorded 2026-10-16)

Against the deterministic fixture (`bench.fake_upstream`), `--limit 3`:

| task | result | reason | TTFT | tok/s |
|---|---|---|---|---|
| gen-func-body | fail | missing: `if value < low`, `low`, `if value > high` | 26 ms | — |
| fix-off-by-one | fail | missing: `off-by-one`, `items[0:len(items)]` | 3 ms | — |
| json-schema-answer | fail | no JSON object in the answer | 3 ms | — |

TTFT p95 (approx): 26 ms. The fixture answers `token0 token1 …`, so the *checks* correctly reject it:
the run proves the harness, the checks and the JSONL path, and the render marks it
**not a verdict** (environment `fake-upstream`). tok/s is `—` because the fixture coalesces the
stream — the same guard the benchmark uses.

The CI job `coding-validation-mock` runs exactly this (`--smoke`), so the runner cannot rot silently.

## Hardware procedure (not yet run)

`BLOCKED: no GPU hosts in this environment.` On hardware:

1. Baseline: single-host configuration, the model under test, `--environment hardware`, and the
   same number of GPUs as the multi-node configuration (the roadmap's comparison rule).
2. Multi-node: deploy the same model with `nodes_per_replica = 2` (`pp`) and repeat the command.
3. Publish both runs in this file with the raw JSONL appended to the release notes, plus the §5
   thresholds: TTFT p95 ≤ 1.0 s, ITL p95 ≤ 50 ms, ≥ 20 tok/s, and the pass rate.
4. Verdict line: “usable for interactive coding”, “usable for batch/agentic coding with a slower
   first token”, or “not usable”, **with the RTT/configuration it was measured at** — never a bare
   “multi-node is fine”.

## What “usable” means, concretely

- `aider`/Continue-style interactive editing wants TTFT p95 ≤ 1.0 s and ≥ 20 tok/s; a long-context
  agent loop tolerates a slower first token but not a slow token rate, because every tool iteration
  re-sends the growing prefix.
- The prefix-cache hit ratio matters more here than anywhere else: the agentic tasks repeat a large
  prefix every iteration, so `inferna_prefix_cache_hit_ratio` below 0.8 usually shows up as a rising
  TTFT while the answers stay correct (see `docs/roadmap/plans/evidence-w2-cache-aware-routing.md`).
- A configuration that passes the latency thresholds but fails the correctness checks is not
  “usable, just slow” — that is why both are in the same report.