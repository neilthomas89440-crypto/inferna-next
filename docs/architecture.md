# Architecture

## Overview

Inferna Next is a control plane for GPU clusters with a web UI. A central server holds
all state (users, clusters, workers, model catalog, instances) in a database and exposes
a REST API for the UI plus a gRPC service for worker agents. Workers poll the server and
reconcile local inference-engine containers.

## Data flow (worker Sync loop)

```
┌─────────────┐  1. Register (cluster_token, hostname)     ┌──────────────────┐
│   Worker    │ ──────────────────────────────────────────► │      Server      │
│             │        ← worker_id + worker_token           │  (FastAPI+gRPC)  │
│  every 5 s  │  2. Sync (worker_id, worker_token,          │                  │
│             │ ──────────────────────────────────────────► │  • auth token    │
│             │    system, gpus, instance statuses)         │  • upsert worker │
│             │        ← commands (start/stop/delete)       │  • upsert GPUs   │
└──────┬──────┘                                             │  • persist state │
       │  3. reconcile: pull image, create/start container, │  • build commands│
       │     health-probe /v1/models, stop/remove           └──────────────────┘
       ▼
  GPU host (docker: inferna-instance-*)
```

1. **Register** — worker presents the cluster token; server creates (or reuses) the worker
   row, rotates the worker token, returns `worker_id` + `worker_token` + sync interval (5 s).
2. **Sync** — worker authenticates with `worker_token`, reports system info, per-GPU state,
   and the status of every instance it manages. The server persists this and returns
   commands derived from desired state:
   - instance `scheduled` → `start` (with full engine config)
   - instance desired `stopped` but reported running/starting → `stop`
   - instance the worker runs that no longer exists in DB → `delete`
3. **Reconcile** — the worker applies commands with Docker: pull the engine image once per
   tag, create the container (`inferna-instance-<id>`, GPU device requests, host port,
   HF cache volume, HF token env), start it, probe `GET /v1/models` every 10 s until 200
   (600 s timeout), and report `running`/`error`.

Disconnect handling: a server background task marks workers with `last_seen_at` older than
30 s as `disconnected`, and their live instances as `error` ("worker disconnected").

## Scheduling

`POST /api/v1/model-instances` with `gpu_selection: "auto"`:

- candidates = connected workers in the cluster
- per GPU, used VRAM = Σ vram_required_mb of that worker's live instances using the GPU
- free = `gpu.vram_mb − used`; pick the fitting GPU with the **smallest** free ≥ required
  (best-fit)
- no fit → 400 `no GPU with enough free VRAM in cluster`
- manual selection (worker + gpu indexes) is validated (worker connected, indexes exist,
  each fits)
- instance port = lowest free port in `[8010, 8100]` on that worker

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

## Security / RBAC

- Passwords: bcrypt. Sessions: JWT HS256, `{sub: user_id, exp: +24h}`.
- Roles: `admin` (manages users/clusters/workers) and `user` (deploys/stops/deletes
  instances, reads everything). No self-registration.
- Worker gRPC auth: cluster token on Register, per-worker token on Sync (SHA-256 hash stored).
- Inference API keys: `Authorization: Bearer inf-<key>`, SHA-256 hash stored in `api_keys`,
  revocable, default scope `inference`.
- `INFERNA_AUTH_ENABLED=false` disables auth for dev (all requests act as admin).

### Network boundary

- `9091` (gRPC) and `8010–8100` (instance ports) are only reachable from the private network. `8000` (REST API) sits behind a reverse proxy that terminates TLS. Control traffic is worker→server: workers dial the gRPC endpoint on `9091`; the server never dials workers for control.
- The `/v1` inference gateway (server root, `:8000/v1`) proxies client inference requests **server→worker** over TCP to `8010–8100` inside the private network. The server reaches a worker at `worker.address` (set via `INFERNA_WORKER_ADDRESS`) falling back to the registered hostname. Direct client access to instance ports is no longer required and remains an advanced mode only.
- `docker-compose.yml` is a **dev/demo artifact**: it publishes `9091` on all interfaces so a worker outside the compose network (e.g. an NVIDIA node on another host) can reach it. In production the server host **must** close `9091` with a firewall/security-group rule — verified by scenario 0 of `docs/phase0-validation.md`.
- No TLS/mTLS is implemented in this phase; the private-network boundary is the documented security perimeter.
## Directory layout

```
inferna-next/
├── proto/cluster.proto          # gRPC contract (WorkerService: Register, Sync)
├── server/                      # FastAPI control plane (Python 3.12)
│   └── inferna_server/
│       ├── main.py              # app + lifespan (gRPC task, DB seeding)
│       ├── config.py            # INFERNA_* settings (pydantic-settings)
│       ├── db.py, models.py, schemas.py, auth.py
│       ├── api/                 # REST routers (/api/v1) + gateway.py (/v1), keys.py
│       ├── grpc_server.py       # WorkerService servicer
│       ├── services/            # workers_svc, scheduler, metrics
│       ├── proto/               # generated stubs (committed)
│       └── fixtures/catalog.json# builtin model catalog
├── worker/                      # GPU agent (Python 3.12)
│   └── inferna_worker/
│       ├── main.py              # register + Sync loop + shutdown
│       ├── config.py, gpu.py    # NVIDIA/AMD/mock detection
│       ├── engines/             # base.py flag tables, manager.py reconcile
│       └── proto/               # generated stubs (committed)
├── frontend/                    # React 19 + TS + Vite + Tailwind v4
├── scripts/                     # gen-proto.sh, dev-setup.sh, mock-worker.sh, export-openapi.py
├── deploy/                      # prometheus + grafana provisioning
└── docker-compose*.yml          # full stack, mock, monitoring
```

## Instance state machine

`scheduled` → `starting` → `running` ⇄ `stopped`; any → `error`.
The worker is authoritative for live reality; the server only transitions `scheduled`
(deploy) and `stopped` (stop request) and marks `error` on disconnect/removal.
