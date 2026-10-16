# Fault matrix

What each failure is expected to do, how it is injected, and what the soak asserts afterwards. The
soak (`scripts/soak-multinode.sh`) rotates these every `--fault-every` cycles for 24 h and writes one
JSONL row per action to `artifacts/soak-multinode-<ts>.jsonl`.

The rule for every row: **the group stays controllable and ends stopped and deleted**. A fault that
leaves ranks behind, wedges the control plane, or answers 409/500 ends the soak non-zero — a fault
matrix with a passing average is not a fault matrix.

| Fault | Injection | Expected behaviour | Asserted by the soak |
|---|---|---|---|
| Head killed mid-stream | `docker kill inferna-instance-<rank 0>` on its host | the head reports `error`; the gateway stops routing to it (only rank 0 is a candidate, so the model becomes unavailable rather than serving from a rank that has no API); stop+delete still work | group reaches `stopped`, then no rows for the group remain; no 409/500 |
| Follower killed | `docker kill` on a follower container | the head may lose a peer (engine-level reconnect), but the control plane must not treat the follower as healthy; stop+delete still work | same as above |
| Worker restarted with containers alive | `docker restart inferna-worker` on a rank's host | the new worker process adopts the running containers by their labels (`manager.adopt_existing`) instead of recreating them; reports `running` again | group reaches `stopped` with no duplicate containers |
| Packet loss 5 % mid-generation | `scripts/netem.sh apply wan-25ms-1%` for 30 s (Linux, root) | requests fail or slow down, the group stays controllable, no engine restart storm | stop+delete succeed |
| Server restarted during deploy | restart the server while a group is starting | the worker keeps polling, re-registers, and reconciles from desired state; no orphaned containers | group reaches `running` after the restart |
| Rendezvous head unreachable for a follower | block `8110–8199` between rank hosts | followers fail fast with a dial error in their log tail; the head stays healthy | rank failure reason visible in the UI (per-rank log panel) |
| gRPC interrupted (worker↔server) | `docker kill` the worker process for > 30 s | the disconnect watchdog marks the worker offline and its live instances `error` | states reflect `error`; re-registration returns the worker to `connected` |

## Running it

```bash
# 24 h, faults every 4th cycle, injection enabled (ssh://<host> docker access required)
INFERNA_API=http://server:8000 INFERNA_ADMIN_TOKEN=$JWT \
  scripts/soak-multinode.sh --docker

# smoke: one cycle, no injection (works on any stack, including the local mock one)
scripts/soak-multinode.sh --cycles 1 --api http://localhost:8000 --token "$JWT"
```

Rows 1, 2, 3 and 7 need a Docker host reachable over ssh (`--docker`); row 4 needs Linux + root; rows
5 and 6 need the three-worker topology (`docker-compose.multinode.yml`).

## Status

`BLOCKED: no GPU hosts / no docker in this environment` for rows 1–4, 6 and 7. What *has* been run is
the lifecycle half of every row plus the accounting around it: one full cycle on the local mock stack
(deploy group → both ranks `running` → stop → delete, JSONL below), with injection honestly recorded as
`skipped`. The hardware run fills the matrix; the soak already asserts the invariants, so the first
run on real hosts produces evidence rather than a new script.

```jsonl
{"ts":"2026-10-16T11:18:09+00:00","action":"deploy","result":"ok","duration_s":0.03,"cycle":1,"group":"b2af2f1e-cf09-4cae-8380-1a93206447ca","ranks":2}
{"ts":"2026-10-16T11:18:19+00:00","action":"poll_running","result":"ok","duration_s":10.03,"cycle":1}
{"ts":"2026-10-16T11:18:19+00:00","action":"fault","result":"skipped","error":"kill_head (needs --docker/root)","cycle":1}
{"ts":"2026-10-16T11:18:29+00:00","action":"poll_stopped","result":"ok","duration_s":10.05,"cycle":1}
{"ts":"2026-10-16T11:18:29+00:00","action":"delete","result":"ok","cycle":1}
```