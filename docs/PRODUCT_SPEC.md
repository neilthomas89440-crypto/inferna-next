# Inferna Next — Product Spec

This document is the single source of truth for what Inferna Next is. It was written
from the original product prompt (reproduced verbatim below) plus the scope decisions
listed at the end.

## The prompt (verbatim)

> I want to build a platform for managing a cluster of GPUs to make it easy to deploy and run AI models. It should work like a "Model-as-a-Service" for my own hardware.
>
> The system needs a central server with a web UI that can manage multiple GPU clusters, whether they're on-prem servers or in the cloud. From this UI, users should be able to browse a catalog of popular open-source models and deploy them with just a click. The platform should handle the complicated parts automatically, like picking the best inference engine—like vLLM or SGLang—and tuning it for either low latency or high throughput. It should also support a wide variety of GPUs from NVIDIA, AMD, and others.
>
> For managing it, I'll need enterprise features like user authentication, access controls, and good monitoring dashboards (maybe with Grafana) to see GPU usage and system health. Please build the whole thing to run in Docker containers for easy setup.

## Scope decisions

### In scope

- Central server + web UI managing multiple GPU clusters (on-prem or cloud)
- Model catalog of popular open-source models with one-click deploy
- Automatic engine selection: **vLLM** and **SGLang** only
- Tuning profiles per engine: **latency** and **throughput**
- GPU vendor abstraction: **NVIDIA** (NVML), **AMD** (ROCm), **mock** (demo mode)
- Authentication (JWT) + role-based access control (admin / user)
- OpenAI-compatible inference gateway at `/v1` (chat completions, embeddings, audio
  transcriptions, model list) authenticated with per-user API keys
- Monitoring dashboards via Prometheus + **Grafana**
- Everything runs in **Docker** (server, worker, frontend, monitoring, PostgreSQL)

### Explicitly out of scope (not requested in the prompt)

- CLI tool (`infctl`)
- Generated Python SDK
- Kubernetes operator
- HA / leader election (single server; PostgreSQL used only for storage)
- Redis / queues (no message broker)
- Model replicas / autoscaling
- llama.cpp backend

### Implementation decisions

- Python **3.12**, dependency management via **uv** (no Poetry)
- Backend: FastAPI (REST) + gRPC (server ↔ worker sync, unary polling every 5 s)
- Frontend: React 19 + TypeScript + Vite + Tailwind v4
- Storage: SQLAlchemy 2.0 + Alembic; SQLite (dev) or PostgreSQL (compose)
- Instance port pool per worker host: **8010–8100**
- Exposed ports: 8000 API, 9091 gRPC, 8080 web, 5173 vite dev, 9090 Prometheus, 3000 Grafana
- Model catalog seeded from `server/inferna_server/fixtures/catalog.json`; no custom-model authoring UI in v1
- AUTH_ENABLED=false disables auth entirely (dev escape hatch)
