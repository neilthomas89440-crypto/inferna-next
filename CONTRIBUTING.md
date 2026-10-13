# Contributing to Inferna Next

Thanks for helping improve Inferna Next. This document covers the development loop, the gates CI
enforces, and the conventions the codebase follows. The repository rules in
[AGENTS.md](AGENTS.md) are the detailed reference for code patterns — read them before a large
change.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12 (exact, CI matrix) | managed by [uv](https://docs.astral.sh/uv/) — no pip, no `requirements.txt` |
| Node | 24 (CI) | `npm` with the committed `package-lock.json` (`npm ci`) |
| Docker | any recent | optional: needed for real engine containers, the compose stacks and e2e |
| PostgreSQL | 16 | optional: only for migration checks and concurrency tests (SQLite is the dev default) |

## Setup

```bash
bash scripts/dev-setup.sh
```

This installs Python 3.12 and both packages with uv, regenerates the gRPC stubs, runs `npm install`,
creates `.env` from `.env.example` and applies migrations.

Run the stack locally:

```bash
cd server && uv run uvicorn inferna_server.main:app --reload   # API :8000, gRPC :9091
bash scripts/mock-worker.sh                                     # mock worker (no GPU/docker)
cd frontend && npm run dev                                      # UI :5173 → proxies /api/v1 to :8000
```

## Gates (all enforced by CI)

| Gate | Command |
|---|---|
| Server lint / types / tests | `cd server && uv run ruff check . && uv run pyright && uv run pytest` |
| Worker lint / types / tests | `cd worker && uv run ruff check . && uv run pyright && uv run pytest` |
| Frontend lint / types / unit tests | `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run` |
| gRPC stubs current | `bash scripts/gen-proto.sh && git diff --exit-code` |
| OpenAPI current | `cd server && uv run python ../scripts/export-openapi.py --check` |
| Migrations | `cd server && INFERNA_DATABASE_URL=postgresql+asyncpg://inferna:inferna@localhost:5432/inferna bash ../scripts/check-migrations.sh` |
| Concurrency (real PostgreSQL) | `cd server && uv run pytest tests/test_scheduler_concurrency.py -v` |
| End-to-end | `cd frontend && npx playwright test` against the mock compose stack |

Two artifacts are **generated and committed**, so regenerate them in the same commit as the change
they describe:

- `proto/cluster.proto` → `bash scripts/gen-proto.sh` (stubs in both packages)
- REST schemas/endpoints → `cd server && uv run python ../scripts/export-openapi.py` (`docs/openapi.yaml`)

`pre-commit` hooks are configured in `.pre-commit-config.yaml` for the cheap checks.

## Conventions

**Python** (both packages)

- SQLAlchemy 2.0 async style, explicit `select(...)`, `commit` + `refresh`.
- `structlog` only — no `print`, no stdlib `logging`.
- Pydantic settings with the `INFERNA_` prefix, cached via `get_settings()`; document every new
  variable in `.env.example` and, when shared, in both `config.py` files.
- Errors as `HTTPException(status_code=..., detail="lowercase message")`; gRPC handlers abort with
  the original RPC code and log everything else via `logger.exception`.
- Typed test helpers are prefixed `_`; each test file starts with a docstring describing its scope.

**Frontend**

- Server state only through the typed React Query hooks in `src/api/hooks.ts`; `fetch` lives in
  `src/api/client.ts` (Bearer token + `ApiError{status, detail}`).
- Pages are `src/pages/<Thing>Page.tsx`; keep the existing table/modal/StatCard patterns.

**Never edit generated code** (`*_pb2*.py`, `alembic/versions/` formatting is excluded from lint).

**Commits** follow the prefixes already in the history: `feat:`, `fix:`, `perf:`, `chore:`,
`docs:`, `test:`. One logical change per commit; explain *why* in the body when the diff is not
obvious.

## Branches, plans and scope

- `main` is always green. Work happens on topic branches (`p04-instance-logs`, `w0/multinode-contract`,
  …) — the branch names for planned work are fixed in
  [`docs/roadmap/plans/README.md`](docs/roadmap/plans/README.md).
- Larger work items have an executable plan in `docs/roadmap/plans/`; the plan states the branch,
  the exact files, the tests and the verification commands. If you execute a plan, record the
  observed results in the matching `evidence-<slug>.md` next to it.
- **No scope creep.** Adding retries, telemetry, abstractions or refactors that the plan (or the
  issue) does not ask for is not a contribution — it makes review and rollback harder. The
  “deliberately not doing” list in `docs/roadmap/06-distributed-inference.md` §8 and the README are
  hard boundaries.
- Only one branch may touch a shared file at a time (`proto/cluster.proto`,
  `server/inferna_server/models.py` + `alembic/versions/`, `api/gateway.py`, `services/scaling.py`,
  `services/metrics.py`); the single-writer order is documented in
  `docs/roadmap/plans/roadmap-program-03-06.md`.

## Tests

- Server tests use a fresh in-memory SQLite per test with `app.dependency_overrides`; reusable
  helpers live in `server/tests/conftest.py` (`login`, `auth_headers`, `add_connected_worker`,
  `deployment_factory`) — use them instead of new fixtures.
- Worker tests combine mock-mode assertions with fake-Docker doubles; no real engines or GPUs are
  touched.
- Behaviour that a consumer can observe must be covered: boundaries, error paths, state
  transitions, precedence. Avoid tests that only assert wiring, forwarding, mock echoes, or that a
  function did not raise.
- UI changes: add a vitest test where behaviour is non-trivial and extend the Playwright spec when
  the change is user-visible.

## Pull requests

- Keep the PR scoped to one plan/issue; describe the user-visible effect and the verification you
  actually ran (commands, results).
- Update docs when behaviour changes: README tables, `docs/architecture.md`, `docs/roadmap/*`
  status, `.env.example`.
- For UI changes, include a screenshot or a short recording in the PR description.
- Expect review on correctness, security surface (SSRF guard, auth, Docker socket usage) and
  generated-artifact freshness.

## Reporting bugs and requesting features

Use the issue templates. For bugs, include: what you ran, what happened, what you expected, the
relevant logs (`docker compose logs server worker`), and your platform (OS, GPU vendor/driver,
engine images). For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public
issue.