# Inferna Next

[![CI](https://github.com/neilthomas89440-crypto/inferna-next/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/neilthomas89440-crypto/inferna-next/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg)](https://www.python.org/)
[![Node 24](https://img.shields.io/badge/node-24-339933.svg)](https://nodejs.org/)
[![Docker Compose](https://img.shields.io/badge/docker%20compose-ready-2496ED.svg)](docker-compose.yml)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

Self-hosted GPU cluster orchestration: deploy and serve open-source AI models on your own hardware
with one click — a “Model-as-a-Service” for machines you control.

**Inferna Next** is a control plane for GPU clusters. A central server exposes a web UI and
REST/gRPC APIs; lightweight worker agents run on each GPU host and manage inference engine
containers (vLLM, SGLang). Deploy a model from a curated catalog, and the platform picks the
worker, the GPU, the engine and the tuning profile (low latency or high throughput) for you.
Everything runs in Docker.

- **Read-only tour:** [Architecture](#architecture) · [Roadmap](#roadmap) · [Docs](#documentation)

## Table of contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [Repository layout](#repository-layout)
- [Development](#development)
- [Testing](#testing)
- [Configuration](#configuration)
- [Ports](#ports)
- [Roadmap](#roadmap)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Features

- **Central web UI** to manage multiple GPU clusters — on-prem or cloud
- **Model catalog with one-click deploy** (Qwen, Llama, Phi, DeepSeek, BGE, Whisper, Qwen2.5-VL),
  plus user-defined custom models from any Hugging Face repo
- **Automatic engine selection** — vLLM and SGLang, tuned for `latency` or `throughput` profiles
- **Automatic scheduling** — worker, GPU and host port chosen server-side, with anti-affinity
  across replicas and PostgreSQL-backed concurrency safety
- **Multi-vendor GPU support** — NVIDIA (NVML), AMD (ROCm), plus a mock mode for demos and CI
  (see the [engine matrix](docs/engine-matrix.md) for the verified combinations)
- **OpenAI-compatible gateway** — `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/transcriptions`
  with API-key auth, SSE streaming pass-through and per-model request accounting
- **Enterprise basics** — JWT auth, role-based access control, Prometheus metrics, Grafana dashboards
- **Docker-first** — one `docker compose` command to run the whole stack

## Screenshots

Dashboard and instance groups, captured from the mock stack
(`docker compose -f docker-compose.yml -f docker-compose.mock.yml up`) — no GPU required:

![Dashboard](docs/assets/ui-dashboard.png)

| Instances — deployments, replicas, per-instance state and actions | Model catalog |
|---|---|
| ![Instances](docs/assets/ui-instances.png) | ![Model catalog](docs/assets/ui-models.png) |

## Architecture

```
        ┌─────────────────────────────┐
        │  Browser (React SPA, :8080) │
        └──────────────┬──────────────┘
                       │ HTTP /api/v1
        ┌──────────────▼──────────────┐        ┌───────────────────────┐
        │  Server (FastAPI, :8000)    │◄──────►│  PostgreSQL (:5432)   │
        │  + gRPC (:9091)             │        └───────────────────────┘
        └──────┬──────────────▲───────┘
     gRPC Sync│  (5s polling) │gRPC Register
        ┌──────▼──────────────┴───────┐
        │  Worker agent (per GPU host)│
        └──────┬──────────────────────┘
               │ docker: inferna-instance-* (vLLM/SGLang, ports 8010-8100)
               ▼
           GPU (NVIDIA / AMD / mock)
```

| Component | Responsibility |
|---|---|
| **Server** (`server/`) | FastAPI control plane: auth/RBAC, clusters, workers, model catalog, instance lifecycle, scheduling, Prometheus metrics. Hosts the gRPC registration/sync service too. |
| **Worker** (`worker/`) | Polls the server every 5 s, reports GPU/system state and instance health, and reconciles local engine containers (pull → create → start → health-probe → stop/remove). |
| **Frontend** (`frontend/`) | React 19 + Vite SPA for cluster, worker, catalog, deployment and API-key management. |
| **Gateway** (`api/gateway.py`) | OpenAI-compatible proxy: API-key auth, upstream resolution, SSE pass-through, usage and latency metrics. Mounted at `/v1` when `INFERNA_GATEWAY_ENABLED=true`. |

## How it works

**Desired state, pulled by workers.** The server never pushes to workers. Each worker calls
`WorkerService.Sync` every ~5 s; the server compares its database state with the reported state and
returns commands (`start` / `stop` / `delete`). Unary polling is NAT-friendly and survives server
restarts. A disconnect watchdog marks silent workers offline.

**Scheduling is server-side.** `services/scheduler.py` picks worker + GPUs + host port for a
deploy. Allocation runs under row locks, reserves ports up front, and tolerates concurrent deploys
(tested against real PostgreSQL in `server/tests/test_scheduler_concurrency.py`).

**Engine abstraction is declarative.** `worker/inferna_worker/engines/base.py` holds vLLM/SGLang
flag tables keyed by profile; `manager.py` runs the container lifecycle. `INFERNA_MOCK_ENGINE=true`
simulates engines in-process — no GPU and no Docker socket required.

**SSRF guard on every upstream.** `services/upstream_guard.py` validates worker-registered
addresses (allowlist or private-range policy depending on `INFERNA_ENV`) before the gateway routes
anything to them.

**Single proto contract.** `proto/cluster.proto` is the only source of truth for server↔worker
traffic. Generated stubs are committed in both packages; CI fails on drift
(`bash scripts/gen-proto.sh && git diff --exit-code`).

## Quickstart

### Demo (mock engine, no GPU needed)

```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml up --build
```

Open http://localhost:8080 and log in with `admin` / `inferna`, then deploy a model — the worker
simulates engines in-process.

### Full stack (real GPUs)

```bash
docker compose up --build
```

Worker containers manage engine containers through the host Docker socket. For the most reliable GPU
access, run the worker bare-metal on the GPU host (see [Development](#development)).

### Monitoring

```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml -f docker-compose.monitoring.yml up -d
```

Prometheus on :9090, Grafana on :3000 (`admin` / `inferna-admin`) with the **Inferna Gateway** and
**Inferna Overview** dashboards.

### OpenAI-compatible API

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $INFERNA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-7b-instruct","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

Create API keys in the UI (or `POST /api/v1/api-keys`) and point any OpenAI client at
`http://localhost:8000/v1`.

## Repository layout

| Path | Purpose |
|---|---|
| `server/` | FastAPI API, gRPC `WorkerService`, inference gateway, scheduler, migrations (`alembic/`), tests |
| `worker/` | Per-GPU-host agent: registration, sync loop, engine lifecycle, GPU probes (NVML/ROCm/mock) |
| `frontend/` | React 19 + Vite SPA; `src/api/` holds the fetch wrapper and typed React Query hooks |
| `proto/` | `cluster.proto` — server↔worker contract (stubs are generated and committed) |
| `deploy/` | Grafana provisioning and dashboards |
| `docs/` | Product spec, architecture, engine matrix, OpenAPI spec, roadmap and implementation plans |
| `scripts/` | Dev setup, proto regeneration, migration checks, soak and hardware-smoke helpers |

## Development

```bash
# Windows-friendly bash: uv + Python 3.12 + server/worker deps + proto + npm install + .env + migrations
bash scripts/dev-setup.sh

cd server && uv run uvicorn inferna_server.main:app --reload   # API :8000, gRPC :9091
bash scripts/mock-worker.sh                                     # mock worker (no GPU, no docker)
cd frontend && npm run dev                                      # web UI :5173 (proxies /api/v1 → :8000)
```

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 24 with `npm`, Docker
(optional — only for real engine containers and the compose stacks).

Run against PostgreSQL and apply migrations:

```bash
cd server && uv run alembic upgrade head
```

## Testing

```bash
cd server   && uv run ruff check . && uv run pyright && uv run pytest
cd worker   && uv run ruff check . && uv run pyright && uv run pytest
cd frontend && npm run lint && npx tsc --noEmit && npx vitest run
cd frontend && npx playwright test          # e2e, needs the mock compose stack running
```

Two generated artifacts are checked in CI and must be regenerated when the contract changes:

```bash
bash scripts/gen-proto.sh && git diff --exit-code                  # gRPC stubs current
cd server && uv run python ../scripts/export-openapi.py            # then commit docs/openapi.yaml
```

Concurrency tests require real PostgreSQL:

```bash
cd server && INFERNA_DATABASE_URL=postgresql+asyncpg://inferna:inferna@localhost:5432/inferna \
  uv run pytest tests/test_scheduler_concurrency.py -v
```

## Configuration

All settings are environment variables with the `INFERNA_` prefix (see `.env.example`). Key ones:

| Variable | Default | Purpose |
|---|---|---|
| `INFERNA_DATABASE_URL` | `sqlite:///./inferna.db` | DB connection (compose uses PostgreSQL) |
| `INFERNA_JWT_SECRET` | `inferna-dev-secret` | JWT signing secret |
| `INFERNA_ADMIN_PASSWORD` | `inferna` | seeded admin password |
| `INFERNA_REGISTRATION_TOKEN` | `inferna-registration-token` | worker cluster token |
| `INFERNA_GRPC_PORT` | `9091` | worker gRPC endpoint |
| `INFERNA_GATEWAY_ENABLED` | `false` | mount the OpenAI-compatible gateway at `/v1` |
| `INFERNA_MOCK_ENGINE` | `false` | worker: simulate engines without GPU/docker |
| `INFERNA_VLLM_IMAGE` | `vllm/vllm-openai:v0.8.5` | vLLM engine image |
| `INFERNA_SGLANG_IMAGE` | `lmsysorg/sglang:v0.4.6.post1` | SGLang engine image |
| `INFERNA_HF_TOKEN` | — | HF token for gated models (e.g. Llama) |

Production mode fails fast on default secrets — set `INFERNA_ENV=production` and real values.

## Ports

| Port | Service |
|---|---|
| 8000 | Server REST API + `/metrics` |
| 9091 | Server gRPC (worker registration/sync) |
| 8080 | Web UI (nginx) |
| 5173 | Vite dev server |
| 8010–8100 | Model instances (per worker host) |
| 5432 | PostgreSQL (compose only) |
| 9090 | Prometheus (monitoring) |
| 3000 | Grafana (monitoring) |

## Roadmap

Direction-level roadmap with acceptance criteria: [`docs/roadmap/`](docs/roadmap/README.md).
Executable plans for the remaining work live in [`docs/roadmap/plans/`](docs/roadmap/plans/README.md).

| Stage | Scope | Status |
|---|---|---|
| 0 | Production readiness — config validation, health/readiness, migrations, CI | ✅ delivered |
| 1 | Inference gateway — OpenAI-compatible `/v1`, API keys, SSRF guard, metrics | ✅ delivered |
| 2 | Diagnostics — instance logs and failure reasons in the UI | ✅ delivered |
| 3 | Custom models — add any HF repo to the catalog from the UI | ✅ delivered |
| 4 | Deployments and manual replicas — groups, scale up/down, replacement of errored replicas | ✅ delivered |
| 5 | Usage metering — per-user/model/key statistics, soft quotas | ✅ delivered |
| 6 | Autoscaling — policy on measured load, cooldown, whole-group scaling | 🗺️ planned |
| 7 | Distributed inference — multi-node vLLM/SGLang, parity verdict, coding validation | 🗺️ planned |

## Documentation

- [Product spec](docs/PRODUCT_SPEC.md) — one-prompt source of truth and scope decisions
- [Architecture](docs/architecture.md) — data flow, ports, directory layout
- [Network matrix](docs/network-matrix.md) — port contract, three-worker topology, wide-area envelopes
- [Multi-node parity](docs/multinode-parity.md) · [Coding validation](docs/coding-validation.md) — the measured verdicts and their thresholds
- [Fault matrix](docs/fault-matrix.md) — what each failure must do, and how the 24 h multi-node soak asserts it
- [Roadmap](docs/roadmap/README.md) — stages, acceptance criteria, release contract
- [Implementation plans](docs/roadmap/plans/README.md) — executable plans per remaining stage
- [Engine matrix](docs/engine-matrix.md) — verified engine × image × GPU combinations
- [Phase-0 validation](docs/phase0-validation.md) — what was observed on real hardware
- [OpenAPI spec](docs/openapi.yaml) — regenerated via `scripts/export-openapi.py`

## Reproducing the release checks

The stage-7 gates are scripted, so a clean machine can reproduce them rather than trust a report:

```bash
# latency/throughput of one configuration, and the parity verdict against a saved baseline
scripts/parity.sh --mode smoke  --api http://localhost:9200 --api-key k --model fake-model \
                  --environment fake-upstream            # harness check, any machine
scripts/parity.sh --mode single --admin-token "$JWT" --api-key "$KEY" --model <model> --gpus 8 \
                  --environment hardware --baseline artifacts/parity-baseline.jsonl

# coding help: correctness + latency for the same model
scripts/coding-validation.sh --api http://server:8000 --api-key "$KEY" --model <model> \
                             --environment hardware

# 24 h multi-node lifecycle soak with rotating faults
INFERNA_API=http://server:8000 INFERNA_ADMIN_TOKEN="$JWT" scripts/soak-multinode.sh --docker

# usage aggregates must agree with the gateway counters
python scripts/usage-reconcile.py --api http://server:8000 --token "$JWT" --days 1 \
       --traffic-cmd "bash scripts/bench.sh --api http://server:8000 --api-key $KEY --model <model> \
                      --scenario single --repeats 5 --warmup 1"
```

`scripts/parity.sh` and the soak exit non-zero when a threshold is missed, and every report records
the **environment** it was measured in — a fixture or mock stack is labelled a harness check, never a
verdict.

**Not yet reproducible here** (recorded as `BLOCKED` in the evidence files, with the exact commands):
the hardware half of the multi-node gates (two or more GPU hosts), filling `docs/engine-matrix.md`
and `docs/phase0-validation.md`, and therefore the `v1.0` tag itself. See
[Roadmap](#roadmap) and [CHANGELOG.md](CHANGELOG.md) for the current state.

## Contributing

PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev loop, the gates CI enforces,
and the code and commit conventions. Bug reports and feature requests use the
[issue templates](.github/ISSUE_TEMPLATE).

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md). Known limitation: the
worker gRPC endpoint has no TLS by default and must stay on a private network (tracked in the
roadmap’s release-hardening stage).

## License

Apache-2.0 — see [LICENSE](LICENSE). Model weights and licenses belong to their own artifacts.