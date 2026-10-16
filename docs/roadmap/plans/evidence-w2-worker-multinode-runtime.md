# Evidence — 06 M2.2 worker multinode runtime (`w2/worker-multinode-runtime`)

The worker half of M2.2: the payload M2.1 froze and the server now sends is turned into engine
flags, container networking and rank readiness.

## What landed

| Piece | Behaviour |
|---|---|
| `engines/base.MultiNodeSpec.from_config` | reads `Parallelism`/`Rendezvous`; returns `None` for absent/empty/`single`, so a single-node config builds exactly the old command |
| `engines/base.build_command(config, spec=None, serve_port=…)` | with a spec: replaces `--port` with the head's host port (host networking), drops `--port` on followers (only the head serves), and appends the rendezvous flags — vLLM `--nnodes/--node-rank/--master-addr/--master-port` (+`--pipeline-parallel-size` for pp, `--distributed-executor-backend mp` and a widened `--tensor-parallel-size` for tp), SGLang `--dist-init-addr` with the same rank flags |
| `engines/base.multi_node_env` | `NCCL_SOCKET_IFNAME` (from `INFERNA_WORKER_NETIF`) and `VLLM_HOST_IP` — the parts only a worker can know; `MASTER_ADDR`/`MASTER_PORT` come from the server's `Rendezvous.env` and are merged verbatim |
| `engines/manager` | grouped containers are created with `network_mode="host"` and **no** port mapping (docker-py rejects both together, and NAT breaks NCCL peer connections); ranks are recorded in the manager state |
| `engines/manager._follower_probe` | a follower has no API server: it is reported `running` once its container stays up `INFERNA_MN_RANK_READY_SECONDS` (default 20 s), and `error` if it exits first — with the container log tail as the reason |
| `engines/manager._apply_batch` | teardown ordering: stop/delete commands run with followers first (highest rank) and the head last, so killing the head cannot strand its peers; starts keep the server's order |
| `config` | `INFERNA_MN_RANK_READY_SECONDS`, `INFERNA_WORKER_NETIF` (+ both in `.env.example`) |

## Invariant kept: single-node is untouched

`build_command(config)` returns the pre-multi-node list **exactly** (same order, same values) — the
test compares against a literal copy of the old command — and `_start_sync` still passes
`network_mode=None` with `ports={"8000/tcp": <host port>}`, so a single-node container keeps
listening on the container port while docker maps it to the allocated host port. `serve_port` only
applies on the multi-node path, which is why the parameter is documented as such.

## Verification

| Gate | Result |
|---|---|
| `cd worker && uv run ruff check .` | `All checks passed!` |
| `cd worker && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd worker && uv run pytest -q tests/test_manager_multinode.py` | 13 passed in 1.3 s |
| `cd worker && uv run pytest -q` | 44 passed in 5.4 s (31 + 13 new) |
| real multi-node command line | **BLOCKED: `docker` is not installed on this host** |

New tests: byte-identical single-node command; vLLM pp head keeps the host port and joins the
rendezvous; vLLM pp follower drops `--port`; vLLM tp widens the base TP flag once (no duplicate
flag) and sets the `mp` backend; SGLang pp uses `--dist-init-addr`; env only carries what the worker
owns; `MultiNodeSpec` is `None` for single-node configs; grouped start uses host networking with the
rendezvous env injected; single-node start keeps its port mapping and old command; a follower with a
stable container becomes `running`; a follower whose container exited becomes `error` with a reason;
teardown runs followers before the head (`["follower-2", "follower-1", "head-1"]`); a port-less
instance schedules no probe.

## Probes: three defects found while chasing a hang

The worker suite intermittently stopped mid-file on this host (the same test passing alone, then
hanging for minutes in a bigger process). Root causes, all real and all fixed:

1. **Leaked probe tasks.** Restarting an instance replaced its container but left the previous
   `_health_probe` task polling a container that no longer existed — up to `HEALTH_PROBE_TIMEOUT`
   of dead polling per restart, and the orphan was never cancelled by `shutdown()` (it is no longer
   in `_probe_tasks`). `_start_sync` now cancels the stale probe before recreating.
2. **Port-less instances were probed.** `_health_probe` connected to `127.0.0.1:0` for instances
   without a port; on Windows the proactor loop sits in `finish_connect` instead of failing fast,
   which stalls whatever awaits it. Scheduling now skips a port-less instance (nothing to poll).
3. **Unbounded HTTP attempt.** Each probe attempt is wrapped in `asyncio.wait_for(..., 5 s)`, so a
   wedged connect cannot outlive one interval. (`httpx(timeout=5)` alone was not enough: the
   proactor connect did not honour it.)

Proof the third-party observation was environmental *and* which part was ours: with the fixes the
whole suite runs in 5.4 s; before them, `tests/test_manager_mock.py` alone wedged reproducibly, and
a control run of the same file twice in one process wedged the same way.

## The blocked flag check (plan §M2.2 first action)

The plan requires verifying the engine flags against the pinned images before trusting the argv:

```bash
docker run --rm vllm/vllm-openai:v0.8.5 vllm serve --help | grep -E "nnodes|node-rank|master-addr|master-port|distributed-executor-backend"
docker run --rm lmsysorg/sglang:v0.4.6.post1 python3 -m sglang.launch_server --help | grep -E "nnodes|node-rank|dist-init-addr"
```

`BLOCKED: no docker on this host` — the commands are recorded here so the first hardware run can
close it. Until then the argv is asserted by unit test, not by the engine, and that is exactly the
gap this note exists to keep visible.
