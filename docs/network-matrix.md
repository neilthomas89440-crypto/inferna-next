# Network matrix: ports, topology and wide-area envelopes

The contract that keeps multi-node deployments reachable and firewalled deliberately. Stage 7's
parity verdict is meaningless without it: a rank that cannot dial the rendezvous point looks like a
model that does not work.

## Ports

| Range | Purpose | Who talks to whom |
|---|---|---|
| `9091` | worker gRPC (`Register`/`Sync`) | worker → server (outbound only; no inbound needed on GPU hosts) |
| `8010–8100` | model instances (rank 0 of a group, and every single-node instance) | gateway → head, operators → head |
| `8110–8199` | rendezvous (`MASTER_PORT` of the head, `INFERNA_RENDEZVOUS_PORT_RANGE_*)` | every rank → head |
| `20000–20100` | NCCL peer connections (ephemeral, engine-chosen) | rank ↔ rank, both directions |
| `56000–56100` | NIXL (only if a `pd`-style deployment is used) | rank ↔ rank, both directions |
| `3000` / `9090` | Grafana / Prometheus (monitoring overlay) | operators |
| `5432`, `8000`, `8080` | PostgreSQL, server API, web UI | operators, browser |

Rules that follow from the design:

- **Rank 0 only is exposed.** Followers publish no instance port at all — the gateway filters on
  `node_rank == 0`, and the worker builds their command without `--port`. Opening the whole
  `8010–8100` range to the internet is never necessary.
- **The rendezvous range must be reachable between ranks, nowhere else.** It is a plain TCP port on
  the head; exposing it publicly hands out a free "connect to an unauthenticated engine" primitive.
- **NCCL/MX needs bidirectional, ephemeral connectivity** between GPU hosts. A firewall rule that
  allows only the rendezvous port is enough to make ranks hang during the first collective.
- **Do not put instances behind NAT.** Rank traffic uses host networking precisely because
  port-mapped containers cannot accept peer connections (`docs/roadmap/06-distributed-inference.md`
  §3.3).

Per-worker ranges are disjoint by configuration. In `docker-compose.multinode.yml` (three workers on
one machine) they are `8010–8039`, `8040–8069`, `8070–8099` with matching rendezvous slices, because
the rendezvous port is allocated on the *head's* host and a shared range would collide.

## Verification tools

```bash
# latency + bandwidth between two hosts
scripts/wan-envelope.sh iperf --peer 10.0.0.2

# shape the link (Linux, root); profiles: dc-0.1ms | dc-2ms | wan-25ms-1%
sudo scripts/netem.sh apply wan-25ms-1%
sudo scripts/netem.sh clear
```

## Envelope: what each mode survives

`scripts/wan-envelope.sh budget` prints the ceiling for one hand-off (pp) or one per-token exchange
(tp). Recorded outputs on this machine (2026-10-14):

| scenario | parameters | per step / token | ceiling | verdict |
|---|---|---|---|---|
| pp, intra-DC | 0.5 MB stage, 1 ms RTT, 25 Gbit/s | 2.3 ms | ≈ 431 tok/s | interactive |
| pp, inter-region | 0.5 MB stage, 25 ms RTT, 10 Gbit/s | 50.8 ms | ≈ 20 tok/s | **batch-only** |
| tp, inter-region | 16 KB/token/link, 25 ms RTT, 10 Gbit/s | 25.0 ms | ≈ 40 tok/s | optimistic upper bound — see caveat |

Notes that keep these numbers honest:

- **pp is latency-bound, not bandwidth-bound** at these payloads: the 1 ms → 25 ms RTT difference
  changes the ceiling twentyfold while the wire time barely moves (0.16 ms → 0.40 ms). That is why
  the verdict flips to batch-only, and why the roadmap publishes "unusable above RTT X ms" rather
  than a promise.
- **The tp number is an upper bound.** It counts *one* collective round per token; real engines run
  several per layer, so an interactive verdict here does not mean tensor parallelism is usable over
  a wide area — the roadmap's "no WAN tp" claim stands. The script prints this caveat with the
  number so the table cannot be misread.
- **Startup transfer is a separate cost**: 16 GB of weights costs ≈ 5 s at 25 Gbit/s and ≈ 13 s at
  10 Gbit/s, which is why `INFERNA_MODELS_DIR` should be a local volume per host (the compose
  overlay gives every worker its own) — otherwise the first deploy of each host pays it again.
- Every number above is a **model**, not a measurement: the `iperf` mode measures the link, and the
  M4 parity run measures the real thing. Put measured envelopes in
  `docs/multinode-parity.md` when they exist.

## Topology: three workers on one machine

```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml \
               -f docker-compose.multinode.yml up -d --build
```

The overlay exists because a parallel group places **one rank per host**, and the server keys
workers by hostname/name: without distinct `INFERNA_WORKER_HOSTNAME`s all three containers collapse
into a single worker and a two-node deploy fails with "needs 2 hosts". Each service gets its own
identity, address, port ranges and model volume. `host.docker.internal` is the head address in this
topology — the one address every container can dial on a single machine; with real hosts it is each
worker's own `INFERNA_WORKER_ADDRESS`.

`BLOCKED: docker is not installed in this environment`, so the overlay is validated by parsing it
(services, identities and ranges asserted) rather than by bringing it up. The first run on a Docker
host closes this.