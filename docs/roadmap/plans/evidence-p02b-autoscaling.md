# Evidence — 06 M8 autoscaling (`p02b-autoscaling`, plan `plan-02-release-b-autoscaling.md`)

Release B, Phase 1 **and** Phase 2 in one slice: the window endpoint, the policy loop and
whole-group scaling. The plan’s own ordering called for Phase 2 after M2.2, which is why both fit here.

## What landed

| Piece | Behaviour |
|---|---|
| `PUT /api/v1/deployments/{id}/range` | sets the autoscaling window (`min_replicas`, `max_replicas`, validated `min ≤ max`) and clamps the current count into it. `POST /scale` keeps its Release A meaning — “exactly N”, pinning `min = max` — so the two intents stay separate |
| `services/autoscaling.py` | pure `desired_replicas(current, window, signal, settings)`; `read_signal` reads the gateway’s TTFT histogram (`inferna_ttft_seconds`) and the per-instance active-request gauge; `autoscale_tick` walks deployments with a window; `autoscale_loop` runs it in the lifespan with an in-memory cooldown |
| policy rules | one replica step per tick; grow when TTFT p95 **or** queue per replica exceeds its target; shrink only below `scale_down_ratio × target` (hysteresis) and never below the floor; **no data ⇒ no action**; in-memory cooldown (300 s default) between actions |
| `services/scaling.py` | **group-aware units**: a replica unit is one instance for single-node and one whole parallel group for multi-node; scale-out creates complete groups (cross-group anti-affinity keeps two groups off one host, each group gets its own rendezvous), scale-in stops whole groups — a tick cannot leave half a group |
| `services/scheduler.py` | `allocate_parallel_replicas(..., exclude_worker_ids=…)` — the anti-affinity that makes a second group land on free hosts, with an honest refusal (`no free hosts left for another parallel group`) when there are none |
| metrics | `inferna_autoscale_actions_total{deployment,action}`, `inferna_autoscale_target_replicas{deployment}`, `inferna_autoscale_signal_ratio{deployment,signal}` — all outside `_ALL_GAUGES` |
| config | `INFERNA_AUTOSCALE_ENABLED=false` (off by default), interval 30 s, cooldown 300 s, targets 1000 ms / 4 queued, ratio 0.5 — documented in `.env.example` |

## Verification

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | 259 passed, 2 skipped (245 + 14 new) |
| `cd server && uv run python ../scripts/export-openapi.py --check` | regenerated in this slice (+64 lines) |
| `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run` | clean; 30 passed (29 + 1 new: the window controls send `PUT /deployments/{id}/range` and the badge says `autoscaling off` for a pinned deployment) |
| plan-02 acceptance (policy survives a real trace without oscillating) | **BLOCKED: no GPU hosts** — the replay path is `bench --replay` + `scripts/autoscale-replay.sh` (plan §5); the policy itself is covered by the threshold tests below |
| whole-group scaling end to end | covered locally: scale a two-node deployment to two groups with four hosts (4 ranks, 2 groups, distinct rendezvous, one head per group), scale back and assert the whole second group stops |

New tests (`server/tests/test_autoscaling.py`, 13): growth on latency and on queue; one step per tick
and clamping at the ceiling; the hysteresis band (no action between `ratio × target` and `target`);
no action when both signals are missing; no shrink below the floor; histogram p95 interpolation and
empty histograms; a tick that grows a deployment; cooldown suppression + the action counter; pinned
windows skipped; `_unit_count` counts groups for multi-node; the loop is cancellable; the range
endpoint widens without moving the count, narrows by scaling in, rejects `min > max` (422) and 404s
unknown deployments, 401 without auth.

`server/tests/test_parallel_groups.py`: the old “scaling a group deployment is refused” assertion was
replaced by three tests of the new behaviour — a second *whole* group is created, scaling in stops a
whole group, and having no free hosts reports `no free hosts left for another parallel group`. The
refusal it used to assert is exactly the limitation this slice removes.

## Policy decisions worth knowing

- **Both signals are optional and independent.** TTFT covers the tail (a user waiting too long), the
  queue covers the mean (a replica is saturated). Either one being hot is enough to grow; both must be
  cool to shrink, which is what keeps a transient spike from causing an immediate scale-in.
- **Missing data is not a signal.** A deployment whose histogram is empty (no traffic yet) is left
  alone; an autoscaler that guesses on absent metrics is worse than one that waits a tick.
- **The cooldown lives in memory.** A server restart forgets it, which costs at most one extra
  evaluation — cheaper than writing `last_scaled_at` on every no-op tick, which would change Release
  A semantics and its tests.
- **Group scaling is refused rather than approximated** when hosts run out: the scheduler answers 400
  with the number of free hosts, and the tick records a `failed` action instead of a partial group.
  The tool that fills the hardware gate (`bench --replay`, `scripts/autoscale-replay.sh`) is already
  built — `bench/bench/trace.py` produces the trace format the replay consumes.