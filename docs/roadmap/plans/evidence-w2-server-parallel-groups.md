# Evidence — 06 M2.2 server parallel groups (`w2/server-parallel-groups`)

Turns the frozen contract (M2.1) into something you can deploy: one deploy request creates a
parallel group, each rank is placed on its own host, the head is the only reachable rank, and the
worker is told the topology instead of guessing it.

## What landed

| Piece | Behaviour |
|---|---|
| `services/scheduler.allocate_parallel_replicas` | one rank per host (anti-affinity by construction), head = a host with a routable `address`, largest fitting GPU per rank, instance port on the head only, rendezvous port from the new range |
| `services/scheduler.rendezvous_host` | bare host for `MASTER_ADDR` (strips scheme/port/path; keeps bare IPv6) |
| `api/instances.deploy` | `nodes_per_replica` + `parallel_mode`; creates one `Deployment` and N ranked `ModelInstance` rows sharing a `parallel_group_id`; validation: mode must be `dp/pp/tp/pd` when multi, `single` when not, manual GPU selection and `replicas > 1` refused for multi-node |
| `services/workers_svc._apply_parallelism` | fills `Parallelism`/`Rendezvous` (incl. `MASTER_ADDR`/`MASTER_PORT` env) for grouped instances; single-node configs stay empty — the old contract |
| `api/gateway._resolve_target` | selection keeps the rank-0 filter explicit (followers hold no port, so it is defence in depth) |
| `services/scaling.apply_scale` | refuses to scale a group deployment down to single-node replicas, so a scale action can never create half groups |
| `worker/inferna_worker/config.worker_hostname` (`INFERNA_WORKER_HOSTNAME`) | identity override — needed to run more than one worker on one host (local multi-node stacks and the M3 compose file) |

## Two real bugs the live smoke found

1. **Registration broke on a hostname change.** `register_worker` looked workers up by
   `(cluster, hostname)` while the table is unique on `(cluster, name)` *and*
   `(cluster, hostname)`, so re-registering the same name from a new hostname raised
   `IntegrityError` → the Register RPC answered `INTERNAL` and the worker retried forever
   (observed: eight retries with 5→80 s backoff, `UNIQUE constraint failed: workers.cluster_id,
   workers.name`). Identity is now resolved against both keys, and a value another row already owns
   is never overwritten silently. Two regression tests pin it.
2. **`MASTER_ADDR` carried a URL scheme.** Workers report their address as a URL
   (`http://10.0.0.1`), so the composed rendezvous was `http://127.0.0.1:8110` — no rank could dial
   that. `rendezvous_host()` strips scheme/port/path, with a test for URL, port, bracketed IPv6,
   bare IPv6 and blank input.

## Live mock lifecycle (M2 gate)

Server on SQLite (gateway enabled) + two mock workers on one machine, distinguished by
`INFERNA_WORKER_HOSTNAME=node-a|node-b`:

```
workers: [('node-a', 'connected'), ('node-b', 'connected')]
deploy: 201
  rank 0: state=scheduled port=8010  host=node-a rendezvous=127.0.0.1:8110
  rank 1: state=scheduled port=None  host=node-b rendezvous=127.0.0.1:8110
running: [(0, 'running', 8010), (1, 'running', None)]
stop: 200, 200 → [(0, 'stopped', 'stopped'), (1, 'stopped', 'stopped')]
delete: 204, 204 → instances left: 0
gateway through the group head: 502 {"error": {"message": "upstream unreachable", …}}
```

The 502 is the correct outcome here: routing reached the head (port 8010, the only rank with a
port) and the mock instance has no engine HTTP server. The explicit target assertion lives in
`test_gateway_only_routes_to_the_head` (follower given a port on purpose is still skipped).

## Gate matrix

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | 232 passed, 2 skipped (218 + 14 new: 12 group tests, 2 registration regressions) |
| `cd worker && uv run ruff check . && uv run pyright && uv run pytest -q` | clean; 31 passed |
| `cd server && uv run python ../scripts/export-openapi.py --check` | up to date (+16 lines) |
| live mock lifecycle above | deploy → both ranks running → stop → delete |
| `bash scripts/check-migrations.sh`, `e2e` | CI |

New tests (`server/tests/test_parallel_groups.py`, 12): group creation on two hosts (ranks, one
group id, world size, shared rendezvous, port only on the head), engine config carries
`Parallelism`/`Rendezvous` including the env pair, single-node config stays empty, four validation
cases, not enough hosts, no routable head, gateway routes to the head only, scaling a group is
refused, and the rendezvous-host helper. Plus two registration regressions in
`server/tests/test_workers_sync.py`.
