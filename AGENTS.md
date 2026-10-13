# Repository Guidelines

## Project Overview

Inferna Next is a self-hosted GPU cluster orchestration platform ("Model-as-a-Service" for your own hardware). A FastAPI control plane manages clusters, workers, and model instances; lightweight worker agents on GPU hosts reconcile inference engine containers (vLLM, SGLang); a React SPA provides the management UI. Includes an OpenAI-compatible inference gateway, JWT auth with RBAC, and Prometheus/Grafana monitoring.

Monorepo of three deployable packages:

- `server/` — `inferna-server`: FastAPI REST API + gRPC `WorkerService` + inference gateway (Python 3.12, uv)
- `worker/` — `inferna-worker`: per-GPU-host agent that polls the server via gRPC and manages engine containers through the Docker socket
- `frontend/` — React 19 + Vite + TypeScript SPA (`npm`, not Bun/pnpm)

## Architecture & Data Flow

```
Browser (React SPA :8080/:5173)
   │ HTTP /api/v1 (JWT Bearer)
Server (FastAPI :8000) ─── PostgreSQL (:5432) / SQLite in dev
   │ gRPC :9091 — WorkerService.Register / WorkerService.Sync (unary polling every 5s)
Worker agent (per GPU host)
   │ docker.sock → inferna-instance-* containers (vLLM/SGLang, ports 8010–8100)
GPU (NVIDIA NVML / AMD ROCm / mock)
```

Key flow points an assistant must understand before editing:

- **Desired-state sync**: the server never pushes to workers. The worker calls `Sync` every ~5 s (`worker/inferna_worker/main.py`); the server computes commands (`start`/`stop`/`delete`) from DB desired state (`server/inferna_server/services/workers_svc.py`). A disconnect watchdog marks silent workers offline.
- **Scheduling** lives server-side: `services/scheduler.py` picks worker+GPU+host port for a deploy; concurrency correctness is tested against real Postgres (`tests/test_scheduler_concurrency.py`).
- **Gateway** (`api/gateway.py`, mounted at `/v1` only when `INFERNA_GATEWAY_ENABLED=true`): API-key auth → upstream instance resolution → httpx pass-through incl. SSE streaming; usage/tokens parsed from responses into Prometheus metrics.
- **SSRF guard**: `services/upstream_guard.py` validates every worker-registered upstream URL (allowlist or private-range policy depending on `INFERNA_ENV`). Never bypass it when touching gateway routing.
- **Engine abstraction**: `worker/inferna_worker/engines/base.py` holds declarative vLLM/SGLang flag tables keyed by profile (`latency`/`throughput`); `manager.py` (`InstanceManager`) does pull→create→start→health-probe→stop lifecycle. `INFERNA_MOCK_ENGINE=true` simulates engines in-process (no GPU/docker).
- **Proto contract**: single `proto/cluster.proto`; generated stubs are committed in both `server/inferna_server/proto/` and `worker/inferna_worker/proto/`. After changing the `.proto`, run `bash scripts/gen-proto.sh` and commit stubs — CI fails on drift.

Domain model: `Cluster` → `Worker` (+`WorkerGPU`) → `ModelInstance` (states: live vs terminal, see `LIVE_STATES` in `models.py`); `Model` catalog seeded from `fixtures/catalog.json`; `User` (admin/user roles) and `ApiKey` for gateway auth.

## Key Directories

| Path | Purpose |
|---|---|
| `server/inferna_server/api/` | One router module per resource (`clusters.py`, `keys.py`, `instances.py`, `users.py`, …); `gateway.py` is the OpenAI-compatible proxy |
| `server/inferna_server/services/` | Business logic called by routers and gRPC servicer (`workers_svc`, `scheduler`, `upstream_guard`, `metrics`, `compatibility`) |
| `server/inferna_server/proto/`, `worker/inferna_worker/proto/` | Generated gRPC stubs (committed; never hand-edit) |
| `server/tests/`, `worker/tests/` | Pytest suites |
| `server/alembic/versions/` | Migrations (ruff excludes them; write carefully by hand) |
| `frontend/src/pages/`, `components/` | UI screens and shared components |
| `frontend/src/api/` | `client.ts` fetch wrapper, typed hooks (`hooks.ts`), API types |
| `frontend/e2e/` | Playwright specs |
| `scripts/` | Dev/proto/migration helper scripts |
| `docs/` | Product spec, architecture notes, roadmap phases |

## Development Commands

```bash
# Full dev setup (Windows-friendly bash): uv py3.12 + deps + proto + npm install + .env + migrations
bash scripts/dev-setup.sh

# Server (API :8000, gRPC :9091)
cd server && uv run uvicorn inferna_server.main:app --reload
cd server && uv run alembic upgrade head        # migrations

# Mock worker (no GPU needed)
bash scripts/mock-worker.sh

# Frontend (:5173, proxies /api/v1 → :8000)
cd frontend && npm run dev

# Full stack
docker compose -f docker-compose.yml -f docker-compose.mock.yml up --build   # demo w/o GPUs
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d # + Prometheus :9090 / Grafana :3000
```

## Code Conventions & Common Patterns

Python (both packages):

- Python ≥3.12; ruff (line-length 100, select `E,W,F,I,UP,B,C4,SIM`; `B008` ignored so FastAPI `Depends(...)` defaults are fine) + strict-ish `pyright`.
- Config: pydantic-settings `Settings` class, env prefix `INFERNA_`, cached via `get_settings()` (`functools.lru_cache`). Production mode fail-fast-validates non-default secrets in `config.py`.
- Logging: `structlog` — always `logger = structlog.get_logger(__name__)`; no stdlib logging/print.
- Endpoints: `APIRouter(prefix="/...", tags=[...])`; async def; DI via `Depends(get_db)` and `Depends(get_current_user)` / `Depends(require_admin)`; errors as `raise HTTPException(status_code=..., detail="lowercase message")`; return ORM models with `response_model=` Pydantic schema from `schemas.py`.
- DB: SQLAlchemy 2.0 async style — explicit `select(...)`, `db.execute(...)`, `scalars().all()`, commit + refresh; sessions from `db.get_db` (routers) or `async with SessionLocal()` (gRPC/background tasks).
- gRPC handlers: catch `grpc.RpcError` → `context.abort(exc.code(), ...)`, everything else → `INTERNAL` with `logger.exception`.
- Tests: pytest with `asyncio_mode = "auto"` (plain `async def test_*`, no decorators); docstring at top of each test file describing scope; helpers prefixed `_`.

Frontend:

- React Query (`@tanstack/react-query`) for all server state via typed wrappers in `src/api/hooks.ts`; never raw `fetch` outside `src/api/client.ts` (which centralizes Bearer token + `ApiError{status, detail}`).
- Auth state in `src/context/AuthContext.tsx`; pages under `src/pages/` named `<Thing>Page.tsx`.

Cross-cutting:

- Generated code (`*_pb2*.py`, `alembic/versions/`) is excluded from lint — don't reformat it.
- All env vars share the `INFERNA_` prefix; document any new one in `.env.example` and both `config.py` files if shared.

## Important Files

- `server/inferna_server/main.py` — app entry: lifespan seeds admin/default cluster/catalog, starts gRPC server + watchdogs, mounts routers, `/healthz` `/readyz` `/metrics`
- `server/inferna_server/grpc_server.py` — `WorkerService` servicer + disconnect watchdog
- `server/inferna_server/api/gateway.py` — OpenAI-compatible proxy (largest, most sensitive module)
- `server/inferna_server/services/scheduler.py` / `upstream_guard.py` — allocation logic and SSRF policy
- `server/inferna_server/config.py` — full env-var surface (mirrored in `.env.example`)
- `worker/inferna_worker/main.py` — register/backoff → Sync loop → reconcile
- `proto/cluster.proto` — the single source of truth for server↔worker protocol (`PROTOCOL_VERSION` in `version.py`)
- `frontend/src/api/client.ts` + `hooks.ts` — all HTTP access

## Runtime/Tooling Preferences

- **Python**: 3.12 exactly (CI matrix), managed by **uv** (`uv sync --dev`, `uv run ...`). No pip/requirements files.
- **Node**: 24 (CI), **npm** with committed `package-lock.json` (`npm ci`).
- **Lint/type gates that must pass before PR** (CI enforces all): `uv run ruff check .`, `uv run pyright`, `uv run pytest` (server + worker), `npm run lint`, `npx tsc --noEmit`, plus two generated-artifact checks:
  - `bash scripts/gen-proto.sh && git diff --exit-code` (stubs current)
  - `cd server && uv run python ../scripts/export-openapi.py --check` (OpenAPI spec current — rerun after changing any REST endpoint/schema)

## Testing & QA

```bash
cd server  && uv run pytest                                   # unit/API tests (in-memory SQLite)
cd worker  && uv run pytest
cd frontend && npx vitest run                                  # component/unit (jsdom)
cd frontend && npx playwright test                             # e2e (needs docker stack up; see CI e2e job)
cd server  && INFERNA_DATABASE_URL=postgresql+asyncpg://... uv run pytest tests/test_scheduler_concurrency.py -v
```

- Server tests: `conftest.py` builds a fresh in-memory SQLite (`StaticPool`) per test, overrides `get_db` via `app.dependency_overrides`, seeds admin user + default cluster + model catalog; helpers `login(client, u, p)` / `auth_headers(token)` / `add_connected_worker(...)`. Use these instead of reinventing fixtures.
- Scheduler concurrency tests intentionally require real PostgreSQL (advisory-lock behavior differs on SQLite).
- Frontend unit tests in `src/__tests__/` (vitest + Testing Library, setup in `setup.ts`); e2e specs in `e2e/*.spec.ts` cover auth, keys, deploy, smoke — Playwright runs against `vite preview` on :8080 with API on :8000, serialized to 1 worker because specs mutate shared state.
- Migration changes must survive `scripts/check-migrations.sh` (empty upgrade, down/up cycle, upgrade-from-previous-release with legacy data) — update `PREVIOUS_RELEASE_HEAD` there on release.
