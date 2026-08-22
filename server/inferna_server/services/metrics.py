"""Prometheus gauges for worker/GPU/instance state, refreshed from the DB."""

from __future__ import annotations

from collections import Counter

from prometheus_client import Counter as PrometheusCounter
from prometheus_client import Gauge, Histogram
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.models import Cluster, ModelInstance, Worker, WorkerGPU

inferna_workers_online = Gauge("inferna_workers_online", "Workers currently connected", ["cluster"])
inferna_gpus_total = Gauge(
    "inferna_gpus_total", "Total GPUs per worker", ["vendor", "worker", "cluster"]
)
inferna_vram_total_mb = Gauge(
    "inferna_vram_total_mb", "Total VRAM (MB) per worker", ["worker", "cluster"]
)
inferna_vram_used_mb = Gauge(
    "inferna_vram_used_mb", "Used VRAM (MB) per worker", ["worker", "cluster"]
)
inferna_gpu_utilization_pct = Gauge(
    "inferna_gpu_utilization_pct", "GPU utilization % per device", ["worker", "index"]
)
inferna_instances_total = Gauge("inferna_instances_total", "Instances by state", ["state"])

# Gateway metrics (counters/histograms; deliberately NOT in _ALL_GAUGES —
# refresh_gauges must never clear them).
inferna_requests = PrometheusCounter(
    "inferna_requests", "Gateway requests by model and status", ["model", "status"]
)  # prometheus_client appends `_total` -> exposed series is `inferna_requests_total`
inferna_request_duration_seconds = Histogram(
    "inferna_request_duration_seconds", "Time to upstream response headers", ["model"]
)
inferna_tokens = PrometheusCounter(
    "inferna_tokens", "Tokens served by kind", ["model", "kind"]  # kind: prompt | completion
)  # exposed as `inferna_tokens_total`

_ALL_GAUGES = (
    inferna_workers_online,
    inferna_gpus_total,
    inferna_vram_total_mb,
    inferna_vram_used_mb,
    inferna_gpu_utilization_pct,
    inferna_instances_total,
)


async def refresh_gauges(db: AsyncSession) -> None:
    """Recompute all worker-state gauges from current DB state."""
    for gauge in _ALL_GAUGES:
        gauge.clear()

    workers = (
        (await db.execute(select(Worker).where(Worker.cluster_id.isnot(None)))).scalars().all()
    )
    instances = (await db.execute(select(ModelInstance))).scalars().all()
    gpus = (await db.execute(select(WorkerGPU))).scalars().all()

    clusters = {c.id: c.name for c in (await db.execute(select(Cluster))).scalars().all()}

    gpus_by_worker: dict = {}
    for gpu in gpus:
        gpus_by_worker.setdefault(gpu.worker_id, []).append(gpu)

    online_per_cluster: Counter = Counter()
    for worker in workers:
        cluster = clusters.get(worker.cluster_id, "unknown")
        worker_label = worker.name
        if worker.state == "connected":
            online_per_cluster[cluster] += 1
        worker_gpus = gpus_by_worker.get(worker.id, [])
        inferna_gpus_total.labels("", worker_label, cluster).set(len(worker_gpus))
        total = sum(g.vram_mb for g in worker_gpus)
        used = sum(g.used_vram_mb for g in worker_gpus)
        inferna_vram_total_mb.labels(worker_label, cluster).set(total)
        inferna_vram_used_mb.labels(worker_label, cluster).set(used)
        for gpu in worker_gpus:
            inferna_gpu_utilization_pct.labels(worker_label, str(gpu.index)).set(
                gpu.utilization_pct
            )
            inferna_gpus_total.labels(gpu.vendor, worker_label, cluster).set(1)
    for cluster, count in online_per_cluster.items():
        inferna_workers_online.labels(cluster).set(count)

    for state, count in Counter(i.state for i in instances).items():
        inferna_instances_total.labels(state).set(count)
