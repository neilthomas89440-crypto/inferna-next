# Implementation plans

Executable plans for the remaining roadmap directions. Roadmap documents
(`docs/roadmap/0*.md`) state *requirements and thresholds*; these plans state *how and in what
order* they are implemented, with file/line anchors into the codebase.

Start here: **[roadmap-program-03-06.md](roadmap-program-03-06.md)** — waves, dependencies,
single-writer map for shared files (`proto/cluster.proto`, `models.py`/`alembic`, `api/gateway.py`,
`services/scaling.py`), gates and the definition of “program closed”.

| Plan | Roadmap direction | Branch | Order |
|---|---|---|---|
| [plan-04-instance-logs.md](plan-04-instance-logs.md) | Stage 2 — diagnostics (`04-instance-logs.md`) | `p04-instance-logs` | 1 |
| [plan-03-custom-models.md](plan-03-custom-models.md) | Stage 3 — custom models (`03-custom-models.md`) | `p03-custom-models` | 2 |
| [plan-05-usage-metering.md](plan-05-usage-metering.md) | Stage 5 — usage + quotas (`05-usage-metering.md`) | `p05-usage-metering` | 3 |
| [plan-06-distributed-inference.md](plan-06-distributed-inference.md) | Stage 7 — distributed inference, M0–M9 (`06-distributed-inference.md`) | `w0…w5` (see §7 of the roadmap) | 4 |
| [plan-02-release-b-autoscaling.md](plan-02-release-b-autoscaling.md) | Stage 6 — autoscaling (`02-replicas-autoscaling.md`, Release B) | `p02b-autoscaling` | after `06` M2.2 |

Rules that apply to every plan:

- One session = one plan = one branch. Parallel branches only where the file sets are disjoint
  (see the single-writer map in the program plan).
- Each step ends with its own checks; a plan ends with its Verification list and an
  `evidence-<slug>.md` file (committed next to the plan).
- Mock-only gates are always closed; gates that need real GPU hosts are recorded as
  `BLOCKED: no two-GPU-host environment available` and the wave continues.
- No scope beyond the plan: the “deliberately not doing” list of `docs/roadmap/06-distributed-inference.md` §8
  and the repository `README` is a hard boundary.

## Superseded

[`superseded/multi-node-mvp-plan.md`](superseded/multi-node-mvp-plan.md) — the first multi-node
MVP slice plan. Rejected (no `w0/multinode-contract`, rendezvous stored in `error_detail`, manual
node/host/port selection, bench inside `server/`, mock OpenAI server, “coding validation” claimed
without M6). Kept for history; replaced by `plan-06-distributed-inference.md`.