# Changelog

All notable changes to Inferna Next. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions are the package versions in `server/inferna_server/version.py` and
`worker/inferna_worker/version.py`. Evidence for each stage (commands, outputs, what was blocked) is
in `docs/roadmap/plans/evidence-*.md`.

## [Unreleased] — stage 7 (distributed inference), milestones M0–M8

The multi-node work, one milestone per section. Measured verdicts live in
`docs/multinode-parity.md` and `docs/coding-validation.md`; the hardware gates are still open.

### Added

- **Measurement harness** (`bench/`): streaming client (TTFT, ITL, tok/s), deterministic load shapes
  (concurrency sweep, agentic loop, 32k/128k long context, TTFT-only), request-trace recording and
  replay, and a deterministic OpenAI-compatible fixture for self-checks.
- **Serving telemetry** (gateway): `inferna_ttft_seconds`, `inferna_inter_token_seconds`,
  `inferna_output_tps` histograms labelled by model/engine/parallelism, plus Grafana panels.
- **Multi-node contract**: `Parallelism`/`Rendezvous` in `proto/cluster.proto`, group columns and a
  partial unique index on `(parallel_group_id, node_rank)`, `nodes_per_replica` / `parallel_mode` on
  deployments, and the rendezvous port range `8110–8199`.
- **Parallel groups**: deploys place one rank per host (head = a host with a routable address, the
  only rank with an instance port), the worker translates the topology into vLLM/SGLang flags and
  runs grouped containers with host networking, and the gateway selects rank 0 only.
- **Network topology**: `docker-compose.multinode.yml` (three workers on one machine), `netem`
  profiles, a wide-area envelope calculator, and the port/firewall contract in
  `docs/network-matrix.md`.
- **Parity verdict** (`scripts/parity.sh`): one command per configuration with `R_tps`/`R_p95`
  against the single-node baseline, and a verdict only for hardware rows.
- **Prefix-cache routing**: the gateway prefers the replica that already holds a prompt’s prefix,
  with `inferna_prefix_cache_hit_ratio`.
- **Coding validation** (`scripts/coding-validation.sh`): 12 deterministic tasks with text checks and
  two opt-in `pytest` tasks, plus a CI harness check.
- **Fault matrix and soak** (`scripts/soak-multinode.sh`): rotating faults with invariants asserted
  every cycle (no 409/500, group stops, no ranks left behind).
- **Autoscaling**: `PUT /api/v1/deployments/{id}/range` (window) and a policy loop on measured load
  (TTFT p95 + queue per replica) with hysteresis and cooldown; scaling moves **whole groups**.
- **Diagnostics**: `?live=true` waits for the worker’s next Sync and reports `stale=true` if it does
  not arrive; `?lines=N` caps the tail; per-rank logs work because every rank is its own instance.
- **gRPC transport security**: `INFERNA_GRPC_TLS_CERT/KEY/CA` and `…_REQUIRE_CLIENT_CERT` for TLS or
  mTLS on `9091`, with a worker-side CA setting; plaintext stays the documented private-network
  default.
- **UI**: parallel groups (rank badges, rendezvous, endpoint only on the head), `nodes_per_replica` +
  parallel mode in the deploy dialog, autoscaling window controls, usage page, custom-model
  management, per-instance log panel.

### Changed

- **Worker identity** is resolved against both `(cluster, name)` and `(cluster, hostname)`; a
  re-registration from a new hostname used to raise `IntegrityError` and loop the worker forever.
- **Gateway accounting** records on completion, upstream failure, client disconnect and 502 —
  previously tokens were lost whenever a client hung up.
- **Throughput is reported as “not measurable”** when the stream arrives coalesced instead of
  publishing an inflated tokens/second.

### Fixed

- Gateway auth compared a naive SQLite datetime with an aware `utcnow()`, so every request after the
  first one per key returned 500 on the dev/e2e stack.
- `MASTER_ADDR` carried a URL scheme (`http://host:port`), which no rank can dial.
- Orphaned health probes after an instance restart, probes against port 0, and unbounded probe
  attempts (which wedged the worker event loop on Windows).
- PostgreSQL migration downgrade used a table rebuild that dropped `users_pkey` while foreign keys
  referenced it.

## [0.2.0] — stages 0–6

### Added

- **Stage 0** production readiness: config validation with production fail-fast, `/healthz` +
  `/readyz` (database, schema head, gRPC), Alembic migrations, CI with lint/typecheck/tests,
  migrations and generated-artifact checks.
- **Stage 1** inference gateway: OpenAI-compatible `/v1` (chat, embeddings, audio), API keys with
  hashed storage, SSRF guard on every upstream, SSE pass-through, Prometheus metrics.
- **Stage 2** diagnostics: per-instance log snapshots reported with each Sync and shown in the UI,
  with the failure reason visible without SSH.
- **Stage 3** custom models: add any compatible Hugging Face repository from the UI (engines derived
  from the category, builtin names reserved, deletion guarded).
- **Stage 4** deployments and replicas: replica groups, scale up/down, replacement of errored
  replicas, per-deployment endpoints.
- **Stage 5** usage and quotas: per user/key/model/day aggregates folded from gateway traffic,
  `GET /api/v1/usage`, soft daily limits, and a reconciliation script against the Prometheus
  counters.
- **Stage 6** autoscaling Release A was the replica range; Release B (policy) is in the unreleased
  section above.