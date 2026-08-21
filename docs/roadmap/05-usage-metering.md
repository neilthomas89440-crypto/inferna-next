# Phase 5 — Usage Metering

## Problem

There is no consumption data: who, how many requests, how many tokens, which
models. Phase-1 Prometheus metrics are aggregates without a per-user breakdown
or history.

## Scope

### Server

- `UsageRecord` table (user_id, model_id, period (day/hour), requests,
  tokens_in/out, avg_latency_ms, gpu_hours) — aggregation.
- The gateway (phase 1) writes a compact event per request; a background
  aggregator (modeled on `mark_disconnected` in `services/workers_svc.py`)
  folds events into `UsageRecord`.
- `GET /api/v1/usage` — own statistics; admins see all users.
- Quotas (soft request/token limits per user) — optional second slice.

### Frontend

- Usage page: request/token charts by model and time.

### Monitoring

- Panels in the phase-1 "Usage" dashboard.

## Out of scope

Billing and payments — accounting only.

## Acceptance criteria

- After gateway requests, per-user/model/day statistics are visible.
- Data matches Prometheus counters within tolerance (streams without a
  usage-chunk are counted by requests).
