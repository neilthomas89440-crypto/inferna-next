"""Autoscaling: policy thresholds, signal reading, tick behaviour and the window endpoint."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from prometheus_client import REGISTRY
from sqlalchemy import select

from inferna_server.config import Settings
from inferna_server.models import Cluster, Deployment, Model, ModelInstance
from inferna_server.services import autoscaling
from inferna_server.services.autoscaling import Signal, desired_replicas, hist_p95


def _settings(**overrides) -> Settings:
    payload = {
        "autoscale_enabled": True,
        "autoscale_interval_seconds": 0.01,
        "autoscale_cooldown_seconds": 300.0,
        "autoscale_target_ttft_ms": 1000.0,
        "autoscale_target_queue": 4.0,
        "autoscale_scale_down_ratio": 0.5,
    }
    payload.update(overrides)
    return Settings(**payload)


def _signal(**overrides) -> Signal:
    payload = {"ttft_p95_ms": 500.0, "queue_per_replica": 1.0, "serving_replicas": 1}
    payload.update(overrides)
    return Signal(**payload)


def test_policy_scales_up_on_latency_or_queue() -> None:
    settings = _settings()

    slow = desired_replicas(current=1, window=(1, 4), signal=_signal(ttft_p95_ms=1500.0),
                            settings=settings)
    queued = desired_replicas(current=1, window=(1, 4), signal=_signal(queue_per_replica=9.0),
                              settings=settings)

    assert slow == 2
    assert queued == 2


def test_policy_moves_one_step_and_respects_the_window() -> None:
    settings = _settings()

    assert desired_replicas(current=1, window=(1, 8), signal=_signal(ttft_p95_ms=5000.0),
                            settings=settings) == 2
    assert desired_replicas(current=4, window=(1, 4), signal=_signal(ttft_p95_ms=5000.0),
                            settings=settings) == 4


def test_policy_keeps_a_hysteresis_band() -> None:
    settings = _settings()

    # Between the target and half of it: neither busy enough to grow nor idle enough to shrink.
    band = desired_replicas(current=2, window=(1, 4), signal=_signal(ttft_p95_ms=800.0,
                                                                     queue_per_replica=3.0),
                            settings=settings)
    idle = desired_replicas(current=2, window=(1, 4), signal=_signal(ttft_p95_ms=200.0,
                                                                     queue_per_replica=0.5),
                            settings=settings)

    assert band == 2
    assert idle == 1


def test_policy_never_acts_on_missing_data() -> None:
    settings = _settings()

    blind = desired_replicas(
        current=3, window=(1, 4), signal=_signal(ttft_p95_ms=None, queue_per_replica=None),
        settings=settings,
    )

    assert blind == 3


def test_policy_does_not_shrink_below_the_floor() -> None:
    settings = _settings()

    assert desired_replicas(current=1, window=(1, 3),
                            signal=_signal(ttft_p95_ms=10.0, queue_per_replica=0.0),
                            settings=settings) == 1


def test_hist_p95_interpolates_and_handles_empty_histograms() -> None:
    buckets = (0.1, 0.5, 1.0, 2.0)

    assert hist_p95(buckets, [50, 80, 95, 100]) == pytest.approx(1000.0)
    assert hist_p95(buckets, [0, 0, 0, 0]) is None
    assert hist_p95(buckets, []) is None


# --- tick ---


async def _deployment(db, *, nodes: int = 1, replicas: int = 1, window=(1, 4)) -> Deployment:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = (
        await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-0.5B-Instruct"))
    ).scalar_one()
    worker = await add_connected_worker(db, cluster.id, name=f"auto-{uuid.uuid4().hex[:6]}")
    deployment = Deployment(
        model_id=model.id,
        cluster_id=cluster.id,
        engine="vllm",
        profile="latency",
        min_replicas=window[0],
        max_replicas=window[1],
        nodes_per_replica=nodes,
        parallel_mode="pp" if nodes > 1 else "single",
    )
    db.add(deployment)
    await db.flush()
    group = uuid.uuid4() if nodes > 1 else None
    for index in range(replicas):
        for rank in range(nodes):
            db.add(
                ModelInstance(
                    model_id=model.id,
                    cluster_id=cluster.id,
                    worker_id=worker.id,
                    deployment=deployment,
                    engine="vllm",
                    profile="latency",
                    gpu_indexes=[0],
                    state="running",
                    desired_state="running",
                    port=8010 + index + rank,
                    parallel_group_id=group if group is not None else (uuid.uuid4() if nodes > 1 else None),
                    node_rank=rank if nodes > 1 else None,
                    world_size=nodes if nodes > 1 else None,
                    parallel_mode="pp" if nodes > 1 else None,
                    rendezvous="10.0.0.1:8110" if nodes > 1 else None,
                )
            )
    await db.commit()
    await db.refresh(deployment, attribute_names=["instances"])
    return deployment


@pytest.fixture(autouse=True)
def _no_real_metrics(monkeypatch):
    """Signals are stubbed per test: a tick must be testable without traffic."""
    monkeypatch.setattr(autoscaling, "read_signal", lambda deployment, instances: _signal())


async def test_tick_scales_up_when_the_signal_is_hot(db, monkeypatch) -> None:
    deployment = await _deployment(db, replicas=1)
    monkeypatch.setattr(
        autoscaling, "read_signal", lambda deployment, instances: _signal(ttft_p95_ms=5000.0)
    )

    actions = await autoscaling.autoscale_tick(db, {})

    assert [(str(action[0]), action[1], action[2]) for action in actions] == [
        (str(deployment.id), "up", 2)
    ]
    await db.refresh(deployment, attribute_names=["instances"])
    assert len(deployment.instances) == 2


async def test_tick_acts_on_a_fresh_clock(db, monkeypatch) -> None:
    """Regression: a container whose monotonic clock is younger than the cooldown must still act.

    The tick used `cooldown.get(id, 0.0)` and compared `now - 0.0 >= cooldown`, so on a machine
    with less than 300 s of uptime every action was suppressed (green locally, red in CI).
    """
    await _deployment(db, replicas=1)
    monkeypatch.setattr(
        autoscaling, "read_signal", lambda deployment, instances: _signal(ttft_p95_ms=5000.0)
    )

    actions = await autoscaling.autoscale_tick(db, {}, now=0.5)

    assert len(actions) == 1


async def test_tick_respects_the_cooldown(db, monkeypatch) -> None:
    deployment = await _deployment(db, replicas=1)
    monkeypatch.setattr(
        autoscaling, "read_signal", lambda deployment, instances: _signal(ttft_p95_ms=5000.0)
    )
    cooldown: dict[uuid.UUID, float] = {}

    first = await autoscaling.autoscale_tick(db, cooldown, now=1000.0)
    second = await autoscaling.autoscale_tick(db, cooldown, now=1000.0 + 1)

    assert len(first) == 1
    assert second == []  # inside the 300 s cooldown
    assert (
        REGISTRY.get_sample_value(
            "inferna_autoscale_actions_total", {"deployment": str(deployment.id), "action": "up"}
        )
        == 1.0
    )


async def test_tick_skips_deployments_without_a_window(db) -> None:
    await _deployment(db, replicas=2, window=(2, 2))

    assert await autoscaling.autoscale_tick(db, {}) == []


async def test_tick_counts_groups_not_ranks_for_multi_node(db, monkeypatch) -> None:
    """A group deployment is scaled in whole groups: one group here, so the target is two."""
    deployment = await _deployment(db, nodes=2, replicas=1)
    monkeypatch.setattr(
        autoscaling, "read_signal", lambda deployment, instances: _signal(ttft_p95_ms=5000.0)
    )

    assert autoscaling._unit_count(deployment) == 1  # two ranks, one unit


async def test_loop_is_cancellable() -> None:
    task = asyncio.create_task(autoscaling.autoscale_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- the window endpoint ---


async def test_range_endpoint_sets_a_window_and_clamps_the_count(client, db, monkeypatch) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    deployment = await _deployment(db, replicas=2, window=(2, 2))

    widened = await client.put(
        f"/api/v1/deployments/{deployment.id}/range",
        json={"min_replicas": 1, "max_replicas": 5},
        headers=auth_headers(token),
    )
    narrowed = await client.put(
        f"/api/v1/deployments/{deployment.id}/range",
        json={"min_replicas": 4, "max_replicas": 6},
        headers=auth_headers(token),
    )
    invalid = await client.put(
        f"/api/v1/deployments/{deployment.id}/range",
        json={"min_replicas": 5, "max_replicas": 2},
        headers=auth_headers(token),
    )
    unknown = await client.put(
        f"/api/v1/deployments/{uuid.uuid4()}/range",
        json={"min_replicas": 1, "max_replicas": 2},
        headers=auth_headers(token),
    )

    assert widened.status_code == 200, widened.text
    assert (widened.json()["min_replicas"], widened.json()["max_replicas"]) == (1, 5)
    assert len(widened.json()["instances"]) == 2  # the count stays where it was
    # Narrowing below the current count scales in to the new floor.
    assert narrowed.status_code == 200, narrowed.text
    assert (narrowed.json()["min_replicas"], narrowed.json()["max_replicas"]) == (4, 6)
    assert len(narrowed.json()["instances"]) == 4
    assert invalid.status_code == 422
    assert unknown.status_code == 404


async def test_range_endpoint_requires_auth(client, db) -> None:
    deployment = await _deployment(db, replicas=1)

    response = await client.put(
        f"/api/v1/deployments/{deployment.id}/range",
        json={"min_replicas": 1, "max_replicas": 2},
    )

    assert response.status_code == 401
