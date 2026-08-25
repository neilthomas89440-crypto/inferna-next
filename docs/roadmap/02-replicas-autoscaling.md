# Phase 2 — Replicas + Autoscaling

## Spec status

Release A — manual Deployment groups, anti-affinity placement, least-loaded
routing — is delivered. Remaining: Release B autoscaling; the
`apply_scale(set_range=False)` hook is already in place.

## Problem

One instance = one point of failure and one GPU's TPS ceiling. The gateway
(phase 1) routes a model to exactly one instance.

## Scope

### Data model

- Minimal version: a new `Deployment` table (model_id, profile, min_replicas,
  max_replicas) plus `ModelInstance.deployment_id`. Instances are members of a
  group; existing single instances become one-replica groups.

### Scheduler

- Anti-affinity: replicas of one group on different workers/GPUs where VRAM
  allows.
- Port from the 8010–8100 pool — `_alloc_port` in `services/scheduler.py`
  already exists.
- Manual placement supports only single replica; multi-replica deploys use
  automatic placement with anti-affinity (Pass 1a different worker → Pass 1b
  different GPU on same worker → Pass 2 fallback).

### Autoscaling

- Manual replicas (deploy with `replicas=N`, scale in/out from the UI) are
  **delivered** (Release A). Remaining: a server background loop (modeled on
  `mark_disconnected` in `services/workers_svc.py`) driven by gateway metrics
  (queue/latency/TPS) → scale up/down with cooldown (Release B).

### Gateway

- Group routing: least-loaded by active request count in the proxy.

## Out of scope

Cross-host tensor parallel (one model across multiple hosts) — a separate
topic requiring an EngineConfig and transport redesign.

## Acceptance criteria

- Deploy with 3 replicas; stopping one leaves gateway traffic working.
- Replicas spread across different GPUs when VRAM allows.
- Scale down releases VRAM and ports without dropping healthy replicas.
