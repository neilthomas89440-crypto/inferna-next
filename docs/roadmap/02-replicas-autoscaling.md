# Phase 2 — Replicas + Autoscaling

## Spec status

In [PRODUCT_SPEC](../PRODUCT_SPEC.md), replicas/autoscaling are explicitly out
of scope. This phase revisits that decision; update the spec before starting.

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

### Autoscaling

- First slice — **manual replicas** (deploy with `replicas=N`, scale in/out from
  the UI): simpler, immediate value. Autoscaling second: a server background
  loop (modeled on `mark_disconnected` in `services/workers_svc.py`) driven by
  gateway metrics (queue/latency/TPS) → scale up/down with cooldown.

### Gateway

- Group routing: least-loaded by active request count in the proxy.

## Out of scope

Cross-host tensor parallel (one model across multiple hosts) — a separate
topic requiring an EngineConfig and transport redesign.

## Acceptance criteria

- Deploy with 3 replicas; stopping one leaves gateway traffic working.
- Replicas spread across different GPUs when VRAM allows.
- Scale down releases VRAM and ports without dropping healthy replicas.
