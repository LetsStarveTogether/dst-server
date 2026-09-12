from pathlib import Path
from typing import cast

import orjson
from pydantic import SecretStr

from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.overrides import (
    ModOverride,
    ModOverrides,
    WorkshopDownloads,
    WorldgenOverride,
)
from dst_server.configuration.world import (
    ForestOverrides,
    WorldOverrides,
)
from tests.lua.helpers import run_lua_process

type TestLuaValue = (
    str | bool | int | float | list[TestLuaValue] | dict[str, TestLuaValue]
)


class UnsupportedWorldOverrides(WorldOverrides):
    value: object


class ModdedWorldOverrides(WorldOverrides):
    task_set: str
    mod_option: str


def source_customize_contract(luajit: str, scripts: Path) -> dict[str, object]:
    root = Path(__file__).parents[2]
    output = run_lua_process(luajit, root / "tests/lua/customize_contract.lua", scripts)
    return cast(dict[str, object], orjson.loads(output))


def schema_definition(
    schema: dict[str, object],
    node: dict[str, object],
) -> dict[str, object]:
    while (reference := node.get("$ref")) is not None:
        definitions = cast(dict[str, dict[str, object]], schema["$defs"])
        node = definitions[cast(str, reference).rsplit("/", 1)[1]]
    return node


def schema_types(
    schema: dict[str, object],
    node: dict[str, object],
) -> set[str]:
    definition = schema_definition(schema, node)
    variants = definition.get("anyOf")
    if variants is not None:
        return set().union(
            *(
                schema_types(schema, variant)
                for variant in cast(list[dict[str, object]], variants)
            )
        )
    return {cast(str, definition["type"])}


def make_cluster() -> ClusterConfig:
    settings = ClusterSettings(
        cluster_name="SDK configuration test",
        offline_cluster=True,
        lan_only_cluster=True,
        autosaver_enabled=False,
        master_ip="127.0.0.1",
        cluster_key=SecretStr("test-cluster-key"),
        max_players=1,
    )
    master = ShardConfig(
        settings=ShardSettings(
            is_master=True,
            id=1,
            master_server_port=27016,
            server_port=10999,
        ),
        world=WorldgenOverride.forest(overrides=ForestOverrides(day="onlyday")),
        mods=ModOverrides(entries={"workshop-42": ModOverride(enabled=True)}),
    )
    caves = ShardConfig(
        settings=ShardSettings(
            is_master=False,
            name="Caves",
            id=2,
            master_server_port=27017,
            server_port=11000,
        ),
        world=WorldgenOverride.cave(),
    )
    return ClusterConfig(
        settings=settings,
        shards={"Master": master, "Caves": caves},
        token=SecretStr("test-token"),
        adminlist="KU_admin\n",
        whitelist="KU_friend\n",
        blocklist="76561198000000000\n",
        downloads=WorkshopDownloads(
            items=frozenset({8}),
            collections=frozenset({99}),
        ),
    )
