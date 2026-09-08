from typing import Any, cast

import pytest
from pydantic import ValidationError

from dst_server import commands as c
from dst_server.api import ClusterAPI, ShardAPI


class Shard(ShardAPI):
    def __init__(self) -> None:
        self.requests: list[c.Request[Any]] = []

    async def invoke[T](self, command: c.Request[T]) -> T:
        self.requests.append(command)
        return cast("T", None)


class Cluster(ClusterAPI):
    def __init__(self) -> None:
        self.requests: list[c.Request[Any]] = []

    async def invoke[T](self, command: c.Request[T]) -> T:
        self.requests.append(command)
        return cast("T", None)


@pytest.mark.asyncio
async def test_shard_facade_builds_shared_requests() -> None:
    shard = Shard()
    await shard.execute("return 1", timeout=17)
    await shard.players.give("KU_example", "log", count=3)
    await shard.players.set_vitals("KU_example", health=0.5)
    await shard.regenerate_shard(preserve_settings=False)
    assert shard.requests == [
        c.Execute(source="return 1", timeout=17),
        c.Give(userid="KU_example", item="log", count=3),
        c.SetVitals(userid="KU_example", health=0.5),
        c.RegenerateShard(preserve_settings=False),
    ]


@pytest.mark.asyncio
async def test_cluster_facade_uses_cluster_response_contracts() -> None:
    cluster = Cluster()
    await cluster.status()
    await cluster.save(timeout=18)
    await cluster.pause(True)
    await cluster.list_players()
    assert cluster.requests == [
        c.ClusterStatusQuery(),
        c.ClusterSave(timeout=18),
        c.ClusterPause(paused=True),
        c.LocatePlayers(),
    ]


@pytest.mark.asyncio
async def test_facade_rejects_invalid_arguments_before_invoke() -> None:
    shard = Shard()
    with pytest.raises(ValidationError):
        await shard.players.give("KU_example", "log", count=65)
    with pytest.raises(ValidationError):
        await shard.save(timeout=0)
    assert shard.requests == []
