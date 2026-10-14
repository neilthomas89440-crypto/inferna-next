# Evidence — 06 M2.1 multinode contract (`w0/multinode-contract`)

The contract other branches build on: what a parallel group *is* in the database and on the wire.
Deliberately behaviour-free — no scheduling, no engine flags, no gateway change. The gate is that
single-node behaviour stays byte-identical.

## What the contract now says

**On the wire** (`proto/cluster.proto`):

- `Parallelism{mode, tensor_parallel, pipeline_parallel, world_size}` and
  `Rendezvous{master_address, master_port, job_id, node_rank, num_nodes, peers, env}`.
- `EngineConfig.parallelism` (8), `.rendezvous` (9), `.env` (10). Absent fields decode as empty
  messages, so an old worker/server pair keeps working: `parallelism.mode == ""`,
  `world_size == 0`, `env == []` — asserted by a test rather than assumed.
- `Rendezvous.env` is the channel for `MASTER_ADDR`/`MASTER_PORT`/`VLLM_HOST_IP`/`NCCL_*`, so the
  server owns the topology and the worker injects it verbatim (invariant §3.4).

**In the database** (`alembic/versions/8a89164e3de6_multinode_contract.py`):

| Object | Meaning |
|---|---|
| `model_instances.parallel_group_id` (nullable, indexed) | a group is one logical replica; every row with a group id is a rank |
| `model_instances.node_rank`, `.world_size`, `.parallel_mode`, `.rendezvous` | rank identity; `rendezvous` is `"host:port"` of rank 0 and the **only** place that address lives |
| `uq_model_instances_group_rank` (partial unique on `parallel_group_id, node_rank`) | one rank per group; a double insert loses here instead of creating two heads. Rows without a group id are not constrained |
| `deployments.nodes_per_replica` (default 1, CHECK 1..8) | desired group size, so a deploy is one row |
| `deployments.parallel_mode` (nullable, CHECK in `single,dp,pp,tp,pd`) | topology wanted for the group |
| `INFERNA_RENDEZVOUS_PORT_RANGE_START/END` = 8110..8199 | kept disjoint from `INFERNA_INSTANCE_PORT_RANGE_*` (8010..8100); asserted by a test, and the port matrix lands in `docs/network-matrix.md` at M3 |

**In the API**: `InstanceOut` gains the five group fields, `DeploymentOut` gains
`nodes_per_replica`/`parallel_mode` — additive, both `null`/`1` for existing rows, so the UI and
existing clients see no change (`docs/openapi.yaml` regenerated, +35 lines).

## Verification

| Gate | Result |
|---|---|
| `bash scripts/gen-proto.sh && git diff --exit-code` | stubs regenerated and committed |
| migration cycle on SQLite (`upgrade head` → `downgrade -1` → `upgrade head`) | clean (the two dialect-sensitive spots — the partial index and a CHECK on `deployments` — use the batch rebuild only on SQLite, plain DDL elsewhere) |
| `cd server && uv run ruff check . && uv run pyright` | `All checks passed!`; `0 errors, 0 warnings` |
| `cd server && uv run pytest -q tests/test_multinode_contract.py` | 9 passed |
| `cd server && uv run pytest -q` | 218 passed, 2 skipped (209 + 9 new) |
| `cd worker && uv run ruff check . && uv run pyright && uv run pytest -q` | clean; 31 passed (unchanged — the additive proto fields need no worker change) |
| `bash scripts/check-migrations.sh` (PostgreSQL), `e2e` | CI rows below |
| `cd server && uv run python ../scripts/export-openapi.py --check` | up to date (+35 lines) |

New tests (`server/tests/test_multinode_contract.py`): single-node rows carry no group columns and
NULL group ids never collide; one rank per group is enforced (IntegrityError); the same rank in two
groups is allowed; a deployment created the old way keeps `nodes_per_replica == 1` and no mode;
`parallel_mode` is constrained to the five values; rendezvous and instance port ranges are disjoint;
`EngineConfig` defaults stay single-node; a full `Parallelism`/`Rendezvous` round-trip survives
serialization; `GET /model-instances` exposes the new fields as `null` for a single-node row.

## Why this is a separate branch

Per the roadmap's branch map, `w0` is the single writer of `proto/cluster.proto` and
`alembic/versions/`; everything downstream (`w2/server-parallel-groups`,
`w2/worker-multinode-runtime`, `w4/diagnostics`, `w4/usage-metering`) branches from its tip. Doing
it as part of a feature branch is what broke the earlier rejected slice: it had no contract, so the
parallel fields could not reach a worker at all.
