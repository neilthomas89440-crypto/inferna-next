# Runtime smoke on the real mock stack

Local stack (no GPU, no docker engine needed): server on SQLite + mock worker + Vite dev UI.

```
cd server && uv run alembic upgrade head    -> b4d7e91c2a58 -> a5646d3ba245 (log_tail)
cd server && INFERNA_GATEWAY_ENABLED=true uv run uvicorn inferna_server.main:app --port 8000
cd worker && INFERNA_MOCK_ENGINE=true uv run python -m inferna_worker   -> "registered with server"
cd frontend && npm run dev                                                   -> :5173
```

Deploy via API (login admin/inferna, POST /api/v1/model-instances, Qwen/Qwen2.5-0.5B-Instruct,
auto): instance reached `running` on port 8010 (same as in the wave-0 run).

With log_tail:

```
GET /api/v1/model-instances/<id>/logs
{"instance_id":"...","state":"running","log_tail":"mock: resolving model weights\nmock: loading model into GPU memory\nmock: engine ready on port 8010","updated_at":"..."}
```

SSE `async def statuses()` did not stall the 5 s Sync loop (mock synthesis path); in the
unit suite the blocking-docker case is covered by `test_statuses_does_not_block_on_docker_logs`.
List endpoint payload unchanged (logs are not part of `InstanceOut`; separate endpoint only).

2026-10-13, commit 92006b1.

# Verification matrix (gate commands)

| Gate | Result |
|---|---|
| `cd worker && uv run ruff check . && uv run pytest -q` | `All checks passed!`; `31 passed in 2.45s` (26 old + 5 new) |
| `cd worker && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run ruff check . && uv run pytest -q` | `All checks passed!`; `180 passed, 2 skipped` (174 + 6 new) |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `bash scripts/gen-proto.sh && git diff --exit-code` | stubs committed, diff clean |
| `cd server && uv run python ../scripts/export-openapi.py --check` | up to date (+55 lines: `/model-instances/{instance_id}/logs`) |
| `cd frontend && npm run lint` | `eslint .` clean (incl. new e2e spec) |
| `cd frontend && npx tsc --noEmit` | clean |
| `cd frontend && npx vitest run` | `18 passed` (15 + 3 new) |
| `bash scripts/check-migrations.sh` (Postgres) | **NOT RUN locally — no Docker/Postgres on this host; CI job `migrations` covers it** |
| `cd frontend && npx playwright test` | **NOT RUN locally — needs the compose stack; CI job `e2e` covers `e2e/instance-logs.spec.ts`** |

# Files changed

- `proto/cluster.proto` — `InstanceStatus.log_tail = 6` (+ regenerated stubs, committed)
- `worker/inferna_worker/engines/manager.py` — `statuses()` → async; `_mock_log_tail()`;
  docker tail via `asyncio.to_thread` for starting/running/error; empty read keeps the previous
  snapshot; `reconcile()` awaits `statuses()` (3 branches)
- `worker/inferna_worker/main.py` — `instances=await manager.statuses()`
- `worker/tests/test_manager_mock.py` — 4 call sites awaited
- `worker/tests/test_manager_logs.py` — new: mock synthesis, error detail, docker tail, snapshot
  survival, non-blocking read
- `server/inferna_server/models.py` — `ModelInstance.log_tail: Text, nullable`
- `server/alembic/versions/a5646d3ba245_instance_log_tail.py` — add/drop column
  (SQLite via batch_alter)
- `server/inferna_server/services/workers_svc.py` — `LOG_TAIL_MAX_CHARS = 8192`;
  `inst.log_tail = status.log_tail[-LOG_TAIL_MAX_CHARS:]` on Sync (only when non-empty)
- `server/inferna_server/schemas.py` — `InstanceLogsOut`
- `server/inferna_server/api/instances.py` — `GET /api/v1/model-instances/{instance_id}/logs`
  (auth required, 404 unknown)
- `server/tests/test_instance_logs.py` — 6 tests (persist, keep-on-empty, cap, endpoint,
  null tail, 404/401)
- `frontend/src/api/types.ts` — `InstanceLogs`
- `frontend/src/api/hooks.ts` — `useInstanceLogs(id)` (LIVE_INTERVAL polling, enabled when open)
- `frontend/src/pages/InstancesPage.tsx` — `Logs` button per row + `LogsDialog` modal
  (state badge, `<pre data-testid="instance-logs">`, empty state, error line)
- `frontend/src/__tests__/InstanceLogs.test.tsx` — 3 tests
- `frontend/e2e/instance-logs.spec.ts` — mock lifecycle → logs visible → running → cleanup