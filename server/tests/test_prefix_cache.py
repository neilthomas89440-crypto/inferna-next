"""Prefix-cache aware routing: prompt blocks, residency map and replica selection."""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import TEST_ADMIN, add_connected_worker, auth_headers, login
from prometheus_client import REGISTRY
from sqlalchemy import select

from inferna_server.api import gateway as gateway_api
from inferna_server.main import app
from inferna_server.models import Cluster, Deployment, Model, ModelInstance
from inferna_server.services.prefix_cache import PrefixCacheIndex, blocks, prompt_text

LONG_PREFIX = "Repository context:\n" + ("cluster inference gpu kernel latency " * 400)


def test_blocks_are_chained_and_deterministic() -> None:
    first = blocks(LONG_PREFIX)
    second = blocks(LONG_PREFIX)

    assert first == second
    assert len(first) == len(LONG_PREFIX) // 512 + 1
    # A shared prefix shares leading blocks; the suffix change only shifts later ones.
    extended = blocks(LONG_PREFIX + " and more")
    assert extended[: len(first) - 1] == first[: len(first) - 1]
    assert extended[-1] != first[-1]


def test_prompt_text_flattens_roles_and_content_parts() -> None:
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "image"}]},
    ]

    text = prompt_text(messages)

    assert "system:be brief" in text
    assert "user:hello" in text
    assert prompt_text(None) == ""
    assert blocks("") == []


def test_overlap_counts_the_leading_run_only() -> None:
    index = PrefixCacheIndex()
    instance = uuid.uuid4()
    index.record(instance, ["a", "b", "c"])

    assert index.overlap(instance, ["a", "b", "c"]) == 3
    assert index.overlap(instance, ["a", "b", "x"]) == 2
    assert index.overlap(instance, ["x", "b", "c"]) == 0
    assert index.overlap(uuid.uuid4(), ["a"]) == 0


def test_residency_expires_by_ttl() -> None:
    """A fake clock, because `time.monotonic()` granularity is ~15.6 ms on Windows."""
    now = [1_000.0]
    index = PrefixCacheIndex(ttl_seconds=1.0, clock=lambda: now[0])
    instance = uuid.uuid4()
    index.record(instance, ["a"])

    assert index.overlap(instance, ["a"]) == 1
    now[0] += 1.5  # past the TTL

    assert index.overlap(instance, ["a"]) == 0


def test_ring_evicts_the_oldest_prefixes() -> None:
    index = PrefixCacheIndex(ttl_seconds=60.0, max_per_instance=2)
    instance = uuid.uuid4()
    for block in ("a", "b", "c"):
        index.record(instance, [block])

    assert index.overlap(instance, ["a"]) == 0  # pushed out by the ring (max 2)
    assert index.overlap(instance, ["b"]) == 1
    assert index.overlap(instance, ["c"]) == 1


def test_hit_ratio_is_none_until_something_is_looked_up() -> None:
    index = PrefixCacheIndex()

    assert index.hit_ratio is None
    index.note_lookup(True)
    index.note_lookup(False)

    assert index.hit_ratio == 0.5


# --- gateway integration ---


@pytest.fixture
async def upstream(client):
    created: list[httpx.AsyncClient] = []

    def install(handler) -> None:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(mock)
        app.state.gateway_client = mock

    yield install
    app.state.gateway_client = None
    for mock in created:
        await mock.aclose()


@pytest.fixture(autouse=True)
def _clean_index():
    gateway_api.prefix_cache._seen.clear()
    gateway_api.prefix_cache.matches = 0
    gateway_api.prefix_cache.lookups = 0
    yield
    gateway_api.prefix_cache._seen.clear()


async def _seed_two_replicas(db, name: str) -> tuple[Model, list[ModelInstance]]:
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    model = Model(
        name=name,
        display_name=name,
        category="llm",
        vram_required_mb=4096,
        requires_hf_token=False,
        supported_engines=["vllm"],
    )
    db.add(model)
    await db.flush()
    deployment = Deployment(
        model_id=model.id, cluster_id=cluster.id, engine="vllm", profile="latency"
    )
    db.add(deployment)
    await db.flush()
    instances: list[ModelInstance] = []
    for index in range(2):
        worker = await add_connected_worker(db, cluster.id, name=f"{name}-w{index}")
        worker.address = "127.0.0.1"
        instance = ModelInstance(
            model_id=model.id,
            cluster_id=cluster.id,
            worker_id=worker.id,
            deployment=deployment,
            engine="vllm",
            profile="latency",
            gpu_indexes=[0],
            state="running",
            port=8010 + index,
        )
        db.add(instance)
        instances.append(instance)
    await db.commit()
    for instance in instances:
        await db.refresh(instance)
    return model, instances


async def _key(client) -> str:
    token = await login(client, TEST_ADMIN["username"], TEST_ADMIN["password"])
    created = await client.post(
        "/api/v1/keys", json={"name": "prefix"}, headers=auth_headers(token)
    )
    return created.json()["key"]


async def test_replica_with_the_resident_prefix_wins(client, upstream, db) -> None:
    model_name = "prefix-model"
    _, instances = await _seed_two_replicas(db, model_name)
    key = await _key(client)
    targets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        targets.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    upstream(handler)
    body = {"model": model_name, "messages": [{"role": "user", "content": LONG_PREFIX}]}

    # First request has no residency anywhere: least-loaded/LRU decides.
    first = await client.post("/v1/chat/completions", headers=auth_headers(key), json=body)
    assert first.status_code == 200
    chosen_port = targets[-1].rsplit(":", 1)[-1].split("/")[0]
    chosen = next(i for i in instances if str(i.port) == chosen_port)

    # Second request repeats the prefix: the replica that served it must win even though the
    # other replica is equally idle.
    second = await client.post("/v1/chat/completions", headers=auth_headers(key), json=body)

    assert second.status_code == 200
    assert targets[-1].rsplit(":", 1)[-1].split("/")[0] == str(chosen.port)
    # The gauge is a cumulative hit rate: the first request was a miss (nothing resident yet),
    # the second one matched — 1 of 2 lookups.
    hit_ratio = REGISTRY.get_sample_value(
        "inferna_prefix_cache_hit_ratio", {"model": model_name}
    )
    assert hit_ratio == 0.5
    assert gateway_api.prefix_cache.matches == 1


async def test_prefix_match_degrades_to_balancing_when_that_replica_is_down(
    client, upstream, db
) -> None:
    model_name = "fallback-model"
    _, instances = await _seed_two_replicas(db, model_name)
    key = await _key(client)
    targets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        targets.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    upstream(handler)
    body = {"model": model_name, "messages": [{"role": "user", "content": LONG_PREFIX}]}
    await client.post("/v1/chat/completions", headers=auth_headers(key), json=body)
    served_port = targets[-1].rsplit(":", 1)[-1].split("/")[0]

    # The replica holding the prefix goes away: the request must still be served by the other one.
    hot = next(i for i in instances if str(i.port) == served_port)
    hot.state = "stopped"
    await db.commit()

    response = await client.post("/v1/chat/completions", headers=auth_headers(key), json=body)

    assert response.status_code == 200
    assert targets[-1].rsplit(":", 1)[-1].split("/")[0] != served_port


async def test_rank_filter_stays_first_for_grouped_models(client, upstream, db) -> None:
    """A follower with a resident prefix must not be chosen over the head (invariant §3.3)."""
    model_name = "grouped-prefix-model"
    model, instances = await _seed_two_replicas(db, model_name)
    group = uuid.uuid4()
    for rank, instance in enumerate(instances):
        instance.parallel_group_id = group
        instance.node_rank = rank
        instance.world_size = 2
        instance.parallel_mode = "pp"
        instance.rendezvous = "127.0.0.1:8110"
    await db.commit()
    key = await _key(client)
    targets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        targets.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    upstream(handler)
    # Only the follower "has" the prefix: the head must still be chosen.
    follow = instances[1]
    gateway_api.prefix_cache.record(follow.id, blocks(prompt_text([{"role": "user", "content": LONG_PREFIX}])))

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers(key),
        json={"model": model_name, "messages": [{"role": "user", "content": LONG_PREFIX}]},
    )

    assert response.status_code == 200
    assert targets[-1].rsplit(":", 1)[-1].split("/")[0] == str(instances[0].port)
