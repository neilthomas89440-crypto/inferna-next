# Evidence — 06 M7 diagnostics (`w4/diagnostics`)

Per-rank logs with an on-demand refresh, inside the pull model the rest of the platform already
follows. Two of the three items in the plan’s first draft turned out to be either already done or
mistaken — both recorded here rather than re-implemented.

## What this slice adds

| Piece | Behaviour |
|---|---|
| `api/instances.GET …/logs?live=true` | waits (bounded, 12 s, poll 0.5 s) for the worker’s next Sync and answers with a fresher snapshot; `stale=true` when the wait expired instead of a timeout error — a five-second-old log tail is still useful |
| `api/instances.GET …/logs?lines=N` | caps the tail at N lines (1..1000, default 100) server-side; `log_tail: null` stays null, because “no logs yet” is not an empty string |
| `services/logs.tail_lines` | the shaping helper (pure, tested) |
| `InstanceLogsOut` | additive `stale` + `lines` fields; the existing contract (snapshot + `updated_at`) is unchanged |

Per-rank logs need no new machinery: **every rank is its own `ModelInstance` row**, so
`ModelInstance.log_tail` (stage 2) and `GET /model-instances/{id}/logs` already address a rank by
`instance_id`. The follower ranks simply have no instance port; their logs arrive with the same Sync.

## Deviations from the plan’s first draft (both deliberate)

1. **No `rpc GetLogs` (server → worker).** It would require *inbound* access to GPU hosts and inverts
   the pull-based control plane (invariant §3.1). The M3 port contract this program wrote says
   explicitly: “worker → server (outbound only; no inbound needed on GPU hosts)”
   (`docs/network-matrix.md`). Adding a server-initiated RPC would contradict a firewall rule we
   document as a design property. `?live=true` gets the same outcome within the poll cadence, and
   `stale=true` makes the trade-off visible instead of hiding it behind a hanging request. The plan’s
   M7 bullet was corrected.
2. **`/readyz` needed nothing.** `main.py:57-86` (`readiness_loop`) already checks the database
   (`SELECT 1`), the Alembic head (`version_num` vs `ScriptDirectory.get_heads()`) and gRPC readiness,
   and `/healthz` stays liveness-only. Verified by reading the code; re-implementing it would have
   been churn.

Deferred, with the reason: **lifecycle event history** (a transitions table). No gate requires it, and
the stage-2 acceptance (“reason visible without SSH”) is already met by `error_detail` plus the log
tail.

## Verification

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | 245 passed, 2 skipped (241 + 4 new) |
| `cd server && uv run python ../scripts/export-openapi.py --check` | regenerated in this slice (+32 lines) |
| live end-to-end (server + mock worker, real Sync) | `live=true` answered in 4.09 s with `stale=false`; `lines=3` in 0.02 s; `lines=5000` → 422 |
| M7 fault matrix / 24 h soak / UI without SSH | sibling branches (`w3/fault-and-soak`, `w3/frontend-parallel`) |

New tests (`server/tests/test_instance_logs.py`, 4): `live=true` returns a freshly synced tail with
`stale=false` (a real `sync_worker` call races the request); `live=true` reports `stale=true` and the
previous snapshot when nothing arrives in the window (wait shortened by monkeypatch — no test sleeps
12 s); `lines=5` returns exactly the last five lines and `lines=5000` is rejected (422); a missing
snapshot stays `null` after capping.

## Live check (real output, 2026-10-16)

`uvicorn` on SQLite + `INFERNA_MOCK_ENGINE=true` worker (`node-a`), one deployed mock instance
`running` on port 8010:

```
GET …/logs?lines=3           200 in 0.02s
  {"log_tail": "mock: resolving model weights\nmock: loading model into GPU memory\nmock: engine ready on port 8010",
   "state": "running", "stale": false, "lines": 3}

GET …/logs?live=true&lines=2 200 in 4.09s
  {"log_tail": "mock: loading model into GPU memory\nmock: engine ready on port 8010",
   "state": "running", "stale": false, "lines": 2}

GET …/logs?lines=5000        422 (le=1000)
```

The `live` wait keys on the **worker heartbeat**, not on `updated_at`: the first attempt measured
`updated_at` and reported `stale=true` after the full 12 s on a healthy, unchanged instance — a Sync
that changes nothing legitimately leaves the row untouched. Waiting for `last_seen_at` to advance
answers the honest question (“has the worker reported since you asked?”) and returned in one poll
interval once an instance’s worker re-registered.

Bonus datapoint for the readiness story, observed by accident: starting the server against an empty
SQLite file **refused to start** with `RuntimeError: database schema missing — run
\`alembic upgrade head\`` (`main.py:_check_schema`) instead of serving 500s — the fail-fast path
works.

The mock worker’s synthesized tail is what makes this checkable without a GPU; on real engines the
tail is the container’s `docker logs --tail` output, read off the Sync loop (stage 2).