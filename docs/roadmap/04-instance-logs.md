# Phase 4 — Instance Logs in UI

## Problem

When an instance is `starting`/`error`, the UI shows only state + detail. The
worker already reads container log tails (`container.logs(tail=LOG_TAIL_LINES)`
in `worker/inferna_worker/engines/manager.py`), but nobody sees those lines.
Diagnostics require SSH access to the worker host.

## Scope

### Protocol (minimal slice)

- Add a `log_tail` field (last N lines) to `InstanceStatus` in
  `proto/cluster.proto` — Sync already polls every 5 s, so adding a field is
  cheap; regenerate via `scripts/gen-proto.sh`.
- Worker: read `docker logs --tail N` for live/error instances in a thread
  (without blocking the Sync loop — the worker-thread pattern already exists).

### Server

- Store the latest `log_tail` (on `ModelInstance` in the DB, or in memory —
  only the last snapshot is needed).
- `GET /api/v1/model-instances/{id}/logs` — return the last lines.

### Frontend

- Logs panel on the instance page (detail/dialog), auto-refresh via the
  existing polling hook (`api/hooks.ts`).

## Out of this phase

- Log streaming (SSE / gRPC stream) — add only if the polling version proves a
  real need.

## Acceptance criteria

- An instance in `error` shows the cause in the UI without accessing the worker
  host.
- Logs update on the `starting` → `running` transition.
- Mock mode synthesizes logs like the rest of its state.
