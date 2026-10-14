"""Frozen multi-node contract: schema shape, group-rank uniqueness, config ranges, proto fields."""

from __future__ import annotations

import uuid

import pytest
from conftest import TEST_ADMIN, auth_headers, login
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from inferna_server.config import get_settings
from inferna_server.models import Cluster, Deployment, Model, ModelInstance
from inferna_server.proto import cluster_pb2


async def _fixtures(db) -> tuple[Cluster, Model, Deployment]:
    """A cluster, a model and one deployment: instances require a deployment (NOT NULL)."""
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = (
        await db.execute(select(Model).where(Model.name == "Qwen/Qwen2.5-7B-Instruct"))
    ).scalar_one()
    deployment = Deployment(
        model_id=model.id, cluster_id=cluster.id, engine="vllm", profile="latency"
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)
    return cluster, model, deployment


def _instance(
    cluster: Cluster, model: Model, deployment: Deployment, **overrides
) -> ModelInstance:
    payload = {
        "model_id": model.id,
        "cluster_id": cluster.id,
        "deployment_id": deployment.id,
        "engine": "vllm",
        "profile": "latency",
        "gpu_indexes": [0],
        "state": "scheduled",
        "desired_state": "running",
        "generation": 1,
    }
    payload.update(overrides)
    return ModelInstance(**payload)


async def test_single_node_rows_keep_working(db) -> None:
    """Rows without a group id are single-node: no group columns, and NULLs never collide."""
    cluster, model, deployment = await _fixtures(db)
    db.add_all(
        [_instance(cluster, model, deployment), _instance(cluster, model, deployment)]
    )
    await db.commit()

    instances = (await db.execute(select(ModelInstance))).scalars().all()

    assert len(instances) == 2
    assert all(instance.parallel_group_id is None for instance in instances)
    assert all(instance.node_rank is None for instance in instances)
    assert all(instance.rendezvous is None for instance in instances)


async def test_one_rank_per_group_is_enforced(db) -> None:
    cluster, model, deployment = await _fixtures(db)
    group = uuid.uuid4()
    db.add(_instance(cluster, model, deployment, parallel_group_id=group, node_rank=0, world_size=2))
    await db.commit()

    db.add(_instance(cluster, model, deployment, parallel_group_id=group, node_rank=0, world_size=2))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_same_rank_in_different_groups_is_allowed(db) -> None:
    cluster, model, deployment = await _fixtures(db)
    db.add_all(
        [
            _instance(cluster, model, deployment, parallel_group_id=uuid.uuid4(), node_rank=0, world_size=2),
            _instance(cluster, model, deployment, parallel_group_id=uuid.uuid4(), node_rank=0, world_size=2),
        ]
    )
    await db.commit()

    assert len((await db.execute(select(ModelInstance))).scalars().all()) == 2


async def test_deployment_group_defaults(db) -> None:
    """A deployment created the old way stays single-node: nodes_per_replica 1, no mode."""
    cluster, model, deployment = await _fixtures(db)
    deployment = Deployment(model_id=model.id, cluster_id=cluster.id, engine="vllm", profile="latency")
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)

    assert deployment.nodes_per_replica == 1
    assert deployment.parallel_mode is None


async def test_parallel_mode_values_are_constrained(db) -> None:
    cluster, model, deployment = await _fixtures(db)
    db.add(
        Deployment(
            model_id=model.id,
            cluster_id=cluster.id,
            engine="vllm",
            profile="latency",
            nodes_per_replica=2,
            parallel_mode="pp",
        )
    )
    await db.commit()

    db.add(
        Deployment(
            model_id=model.id,
            cluster_id=cluster.id,
            engine="vllm",
            profile="latency",
            parallel_mode="nonsense",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


def test_rendezvous_ports_never_overlap_instance_ports() -> None:
    settings = get_settings()

    assert settings.rendezvous_port_range_start == 8110
    assert settings.rendezvous_port_range_end == 8199
    assert not set(settings.rendezvous_port_range) & set(settings.instance_port_range)


def test_engine_config_defaults_stay_single_node() -> None:
    """A config built without the new fields must be indistinguishable from the old contract."""
    config = cluster_pb2.EngineConfig(engine="vllm", model_name="m", profile="latency")

    assert config.parallelism.mode == ""
    assert config.parallelism.world_size == 0
    assert config.rendezvous.node_rank == 0
    assert config.rendezvous.master_address == ""
    assert config.env == []


def test_engine_config_carries_parallelism_and_rendezvous() -> None:
    config = cluster_pb2.EngineConfig(
        engine="vllm",
        model_name="m",
        profile="latency",
        gpu_indexes=[0, 1],
        vram_required_mb=8192,
        port=8010,
        parallelism=cluster_pb2.Parallelism(
            mode="pp", tensor_parallel=2, pipeline_parallel=2, world_size=2
        ),
        rendezvous=cluster_pb2.Rendezvous(
            master_address="10.0.0.1",
            master_port=8110,
            job_id=42,
            node_rank=1,
            num_nodes=2,
            peers=["10.0.0.1"],
            env=["MASTER_ADDR=10.0.0.1", "MASTER_PORT=8110"],
        ),
        env=["NCCL_SOCKET_IFNAME=eth0"],
    )

    decoded = cluster_pb2.EngineConfig.FromString(config.SerializeToString())

    assert decoded.parallelism.mode == "pp"
    assert decoded.parallelism.world_size == 2
    assert decoded.rendezvous.master_address == "10.0.0.1"
    assert decoded.rendezvous.node_rank == 1
    assert list(decoded.rendezvous.env) == ["MASTER_ADDR=10.0.0.1", "MASTER_PORT=8110"]
    assert list(decoded.env) == ["NCCL_SOCKET_IFNAME=eth0"]


async def test_instance_api_exposes_the_new_fields_as_null_for_single_node(
    client, db, deployment_factory
) -> None:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    cluster, model, deployment = await _fixtures(db)
    deployment = await deployment_factory(model.id, cluster.id)
    db.add(_instance(cluster, model, deployment))
    await db.commit()

    response = await client.get("/api/v1/model-instances", headers=auth_headers(token))

    assert response.status_code == 200, response.text
    instance = response.json()[0]
    assert instance["parallel_group_id"] is None
    assert instance["node_rank"] is None
    assert instance["world_size"] is None
    assert instance["parallel_mode"] is None
    assert instance["rendezvous"] is None
