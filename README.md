# Inferna Next

Self-hosted GPU cluster orchestration: deploy and serve open-source AI models on your own hardware with one click — a "Model-as-a-Service" for machines you control.

**Inferna Next** is a control plane for GPU clusters. A central server exposes a web UI and REST/gRPC APIs; lightweight worker agents run on each GPU host and manage inference engine containers (vLLM, SGLang). Deploy a model from a curated catalog, and the platform picks the worker, the GPU, the engine, and the tuning profile (low latency or high throughput) for you. Everything runs in Docker.

## Features

- **Central web UI** to manage multiple GPU clusters — on-prem or cloud
- **Model catalog** with one-click deploy (Qwen, Llama, Phi, DeepSeek, BGE, Whisper, Qwen2.5-VL)
- **Automatic engine selection** — vLLM and SGLang, tuned for latency or throughput profiles
- **Multi-vendor GPU support** — NVIDIA (NVML), AMD (ROCm), plus a mock mode for demos
- **Enterprise basics** — user auth (JWT), role-based access control, monitoring dashboards (Prometheus + Grafana)
- **Docker-first** — single `docker compose` command to run the whole stack

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

- **Server** — FastAPI control plane: auth/RBAC, clusters, workers, model catalog, instance lifecycle, scheduling, Prometheus metrics. Also hosts the gRPC registration/sync service.
- **Worker** — polls the server every 5 s, reports GPU/system state and instance health, and reconciles local engine containers (pull → create → start → health-probe → stop/remove).
- **Sync protocol** — unary gRPC polling (NAT-friendly, survives server restarts). The server returns commands (`start`/`stop`/`delete`) computed from desired state.

## Quickstart

### Demo (mock, no GPU needed)

```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml up --build
```

Open http://localhost:8080, log in with `admin` / `inferna`, deploy a model — the worker simulates engines in-process.

### Full stack (real GPUs)

```bash
docker compose up --build
```

Worker containers manage engine containers through the host Docker socket; for the most reliable GPU access run the worker bare-metal on the GPU host (see below).

### Monitoring

```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml -f docker-compose.monitoring.yml up -d
```

Prometheus on :9090, Grafana on :3000 (`admin` / `inferna-admin`) with the **Inferna Overview** dashboard.

## Development on Windows

```bash
bash scripts/dev-setup.sh      # uv python 3.12 + server/worker deps + proto + npm install + .env + migrations

cd server && uv run uvicorn inferna_server.main:app --reload   # API :8000, gRPC :9091
bash scripts/mock-worker.sh                                     # mock worker (no GPU)
cd frontend && npm run dev                                      # web UI :5173 (proxies /api/v1 → :8000)
```

- `cd server && uv run pytest` — server tests
- `cd worker && uv run pytest` — worker tests
- `cd frontend && npx vitest run && npx tsc --noEmit` — frontend tests + types

## Configuration

All settings are environment variables with the `INFERNA_` prefix (see `.env.example`). Key ones:

| Variable | Default | Purpose |
|---|---|---|
| `INFERNA_DATABASE_URL` | `sqlite:///./inferna.db` | DB connection (compose uses PostgreSQL) |
| `INFERNA_JWT_SECRET` | `inferna-dev-secret` | JWT signing secret |
| `INFERNA_ADMIN_PASSWORD` | `inferna` | seeded admin password |
| `INFERNA_REGISTRATION_TOKEN` | `inferna-registration-token` | worker cluster token |
| `INFERNA_GRPC_PORT` | `9091` | worker gRPC endpoint |
| `INFERNA_MOCK_ENGINE` | `false` | worker: simulate engines without GPU/docker |
| `INFERNA_VLLM_IMAGE` | `vllm/vllm-openai:v0.8.5` | vLLM engine image |
| `INFERNA_SGLANG_IMAGE` | `lmsysorg/sglang:v0.4.6.post1` | SGLang engine image |
| `INFERNA_HF_TOKEN` | — | HF token for gated models (e.g. Llama) |

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

## Docs

- [Product spec](docs/PRODUCT_SPEC.md) — the one-prompt source of truth and scope decisions
- [Architecture](docs/architecture.md) — data flow, ports, directory layout
- [OpenAPI spec](docs/openapi.yaml) — regenerated via `python scripts/export-openapi.py`

## License

Apache-2.0. Model licenses apply to their own artifacts (see the catalog).
