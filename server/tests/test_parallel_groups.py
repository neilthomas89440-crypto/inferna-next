"""Parallel groups: placement, deploy validation, rank-0 routing and the scaling guard."""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from sqlalchemy import select

from inferna_server.main import app
from inferna_server.models import Cluster, Deployment, Model, ModelInstance
from inferna_server.proto import cluster_pb2
from inferna_server.services.workers_svc import _apply_parallelism


async def _cluster(db) -> Cluster:
    return (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()


async def _model_id(db, name: str = "Qwen/Qwen2.5-0.5B-Instruct") -> str:
    model = (await db.execute(select(Model).where(Model.name == name))).scalar_one()
    return str(model.id)


def _body(model_id: str, cluster_id: str, **overrides) -> dict:
    payload = {
        "model_id": model_id,
        "cluster_id": cluster_id,
        "engine": "vllm",
        "profile": "latency",
        "replicas": 1,
        "gpu_selection": "auto",
        "nodes_per_replica": 2,
        "parallel_mode": "pp",
    }
    payload.update(overrides)
    return payload


async def _two_hosts(
    db,
    cluster: Cluster,
    *,
    addresses: tuple[str, str] = ("http://10.0.0.1", "http://10.0.0.2"),
):
    head = await add_connected_worker(db, cluster.id, name="host-a")
    follower = await add_connected_worker(db, cluster.id, name="host-b")
    head.address = addresses[0]
    follower.address = addresses[1]
    await db.commit()
    return head, follower


async def test_deploy_creates_one_group_with_one_head(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    head, follower = await _two_hosts(db, cluster)

    response = await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id)),
        headers=auth_headers(token),
    )

    assert response.status_code == 201, response.text
    instances = response.json()
    assert len(instances) == 2

    ranks = {instance["node_rank"]: instance for instance in instances}
    assert set(ranks) == {0, 1}
    assert len({instance["parallel_group_id"] for instance in instances}) == 1
    assert all(instance["world_size"] == 2 for instance in instances)
    assert all(instance["parallel_mode"] == "pp" for instance in instances)
    # The rendezvous address is recorded once, in `rendezvous`, and it points at the head.
    assert ranks[0]["rendezvous"] == ranks[1]["rendezvous"]
    # A worker reports its address as a URL; MASTER_ADDR must be the bare host.
    assert ranks[0]["rendezvous"] == "10.0.0.1:8110"
    assert ranks[0]["rendezvous"].endswith(":8110")
    # Only the head publishes a port: traffic must never reach a follower.
    assert ranks[0]["port"] == 8010
    assert ranks[1]["port"] is None
    assert {ranks[0]["worker_id"], ranks[1]["worker_id"]} == {str(head.id), str(follower.id)}


async def test_group_ranks_get_an_engine_config_with_topology(client, db) -> None:
    """The contract's whole point: the worker is told the topology instead of guessing it."""
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    await _two_hosts(db, cluster)
    await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id)),
        headers=auth_headers(token),
    )

    grouped = (
        await db.execute(
            select(ModelInstance).where(ModelInstance.parallel_group_id.isnot(None))
        )
    ).scalars().all()
    assert len(grouped) == 2

    configs = {}
    for instance in grouped:
        config = cluster_pb2.EngineConfig()
        _apply_parallelism(config, instance)
        configs[instance.node_rank] = config

    head_config = configs[0]
    follower_config = configs[1]
    assert head_config.parallelism.mode == "pp"
    assert head_config.parallelism.world_size == 2
    assert head_config.parallelism.pipeline_parallel == 2
    assert head_config.rendezvous.master_address == "10.0.0.1"
    assert head_config.rendezvous.master_port == 8110
    assert head_config.rendezvous.node_rank == 0
    assert list(head_config.rendezvous.env) == ["MASTER_ADDR=10.0.0.1", "MASTER_PORT=8110"]
    assert follower_config.rendezvous.node_rank == 1
    assert follower_config.rendezvous.master_address == "10.0.0.1"


async def test_single_node_config_stays_empty(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    await add_connected_worker(db, cluster.id, name="solo")
    created = await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id), nodes_per_replica=1, parallel_mode="single"),
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text

    instance = (
        await db.execute(select(ModelInstance).where(ModelInstance.id == uuid.UUID(created.json()[0]["id"])))
    ).scalar_one()
    config = cluster_pb2.EngineConfig()
    _apply_parallelism(config, instance)

    assert config.parallelism.mode == ""
    assert config.rendezvous.master_address == ""


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"parallel_mode": "single"}, "parallel_mode must be"),
        ({"nodes_per_replica": 1, "parallel_mode": "pp"}, "parallel_mode requires"),
        ({"replicas": 2}, "one replica group"),
        (
            {"gpu_selection": {"worker_id": str(uuid.uuid4()), "gpu_indexes": [0]}},
            "manual gpu selection supports a single replica",
        ),
    ],
)
async def test_multi_node_deploy_validation(client, db, overrides, detail) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    await _two_hosts(db, cluster)

    response = await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id), **overrides),
        headers=auth_headers(token),
    )

    assert response.status_code == 400, response.text
    assert detail in response.json()["detail"]


async def test_multi_node_requires_enough_hosts(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    worker = await add_connected_worker(db, cluster.id, name="only-host")
    worker.address = "10.0.0.1"
    await db.commit()

    response = await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id)),
        headers=auth_headers(token),
    )

    assert response.status_code == 400
    assert "needs 2 hosts" in response.json()["detail"]


async def test_multi_node_requires_a_routable_head(client, db) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    await add_connected_worker(db, cluster.id, name="no-address-a")
    await add_connected_worker(db, cluster.id, name="no-address-b")

    response = await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id)),
        headers=auth_headers(token),
    )

    assert response.status_code == 400
    assert "routable address" in response.json()["detail"]


async def test_gateway_only_routes_to_the_head(client, db) -> None:
    """A follower with a port is still never selected (invariant §3.3)."""
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    head, follower = await _two_hosts(db, cluster)
    model_name = "group-routing-model"
    model = Model(
        name=model_name,
        display_name=model_name,
        category="llm",
        vram_required_mb=4096,
        requires_hf_token=False,
        supported_engines=["vllm"],
    )
    db.add(model)
    await db.flush()
    deployment = Deployment(
        model_id=model.id,
        cluster_id=cluster.id,
        engine="vllm",
        profile="latency",
        nodes_per_replica=2,
        parallel_mode="pp",
    )
    db.add(deployment)
    await db.flush()
    group_id = uuid.uuid4()
    for rank, worker, port in ((0, head, 8010), (1, follower, 8011)):
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
                port=port,
                parallel_group_id=group_id,
                node_rank=rank,
                world_size=2,
                parallel_mode="pp",
                rendezvous="10.0.0.1:8110",
            )
        )
    await db.commit()

    key = (
        await client.post("/api/v1/keys", json={"name": "group"}, headers=auth_headers(token))
    ).json()["key"]
    targets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        targets.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    app.state.gateway_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers(key),
            json={"model": model_name, "messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        await app.state.gateway_client.aclose()
        app.state.gateway_client = None

    assert response.status_code == 200, response.text
    assert len(targets) == 1
    assert ":8010" in targets[0]


async def test_scaling_a_group_deployment_is_refused(client, db) -> None:
    """Scaling single-node replicas inside a group deployment would create half groups."""
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster = await _cluster(db)
    await _two_hosts(db, cluster)
    created = await client.post(
        "/api/v1/model-instances",
        json=_body(await _model_id(db), str(cluster.id)),
        headers=auth_headers(token),
    )
    deployment_id = created.json()[0]["deployment_id"]

    response = await client.post(
        f"/api/v1/deployments/{deployment_id}/scale",
        json={"replicas": 2},
        headers=auth_headers(token),
    )

    assert response.status_code == 400
    assert "whole groups" in response.json()["detail"]


def test_rendezvous_host_strips_scheme_port_and_path() -> None:
    from inferna_server.services.scheduler import rendezvous_host

    assert rendezvous_host("http://10.0.0.1", "host") == "10.0.0.1"
    assert rendezvous_host("https://gpu-7.internal:9000/path", "host") == "gpu-7.internal"
    assert rendezvous_host("10.0.0.1:9000", "host") == "10.0.0.1"
    assert rendezvous_host("[fd00::1]:9000", "host") == "fd00::1"
    assert rendezvous_host("fd00::1", "host") == "fd00::1"  # bare IPv6 keeps its colons
    assert rendezvous_host(None, "fallback-host") == "fallback-host"
    assert rendezvous_host("   ", "fallback-host") == "fallback-host"
