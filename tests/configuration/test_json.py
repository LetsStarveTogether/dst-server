from pathlib import Path
from typing import cast

import orjson
import pytest
from pydantic import SecretStr, ValidationError
from pydantic.json_schema import JsonSchemaMode

from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.overrides import (
    CustomWorldOverrides,
    LevelDataOverride,
    WorldgenOverride,
)
from dst_server.configuration.world import (
    CaveOverrides,
    ForestOverrides,
    LavaArenaOverrides,
    QuagmireOverrides,
    WorldOverrides,
)
from tests.configuration.helpers import schema_definition, schema_types


@pytest.mark.parametrize(
    "overrides",
    [
        WorldOverrides(),
        ForestOverrides(day="longday"),
        CaveOverrides(day="longday"),
        QuagmireOverrides(),
        LavaArenaOverrides(),
        CustomWorldOverrides(custom_density=0.25),
    ],
)
def test_world_json_preserves_typed_sparse_overrides(overrides: WorldOverrides) -> None:
    world = WorldgenOverride(overrides=overrides)
    restored = WorldgenOverride.model_validate_json(
        world.model_dump_json(exclude_unset=True)
    )

    assert type(restored.overrides) is type(overrides)
    assert restored.overrides.model_fields_set == overrides.model_fields_set
    assert restored.render() == world.render()
    assert "kind" not in world.model_dump()["overrides"]


@pytest.mark.parametrize("include", [True, False])
def test_world_json_preserves_nested_field_selection(include: bool) -> None:
    overrides = ForestOverrides(day="onlyday", wildfires="never")
    world = WorldgenOverride(overrides=overrides)
    encoded = world.model_dump_json(
        include={"overrides": {"day"}} if include else None,
        exclude=None if include else {"overrides": {"day"}},
    )

    assert orjson.loads(encoded)["overrides"] == {
        "kind": "forest",
        "values": overrides.model_dump(
            mode="json",
            include={"day"} if include else None,
            exclude=None if include else {"day"},
        ),
    }


@pytest.mark.parametrize("model", [WorldgenOverride, LevelDataOverride])
@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_world_json_schema_describes_tagged_overrides(
    model: type[WorldgenOverride | LevelDataOverride],
    mode: JsonSchemaMode,
) -> None:
    schema = cast(dict[str, object], model.model_json_schema(mode=mode))
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    overrides = schema_definition(schema, properties["overrides"])
    discriminator = cast(dict[str, object], overrides["discriminator"])
    mapping = cast(dict[str, str], discriminator["mapping"])

    assert discriminator["propertyName"] == "kind"
    assert set(mapping) == {
        "world",
        "forest",
        "cave",
        "quagmire",
        "lavaarena",
        "custom",
        "loaded_forest",
        "loaded_cave",
    }
    forest = schema_definition(schema, {"$ref": mapping["forest"]})
    assert set(cast(list[str], forest["required"])) == {"kind", "values"}
    forest_properties = cast(dict[str, dict[str, object]], forest["properties"])
    assert forest_properties["kind"]["const"] == "forest"
    values = schema_definition(schema, forest_properties["values"])
    value_properties = cast(dict[str, dict[str, object]], values["properties"])
    assert schema_types(schema, value_properties["day"]) == {"string"}


def test_configuration_json_preserves_secrets_and_explicit_clears() -> None:
    configuration = ClusterConfig(
        settings=ClusterSettings(
            cluster_key=SecretStr("json-key"), pause_when_empty=False
        ),
        shards={
            "forest": ShardConfig(
                settings=ShardSettings(is_master=True), world=WorldgenOverride.forest()
            )
        },
        token=SecretStr("json-token"),
        adminlist="",
    )
    encoded = configuration.model_dump_json(
        exclude_unset=True, context={"secrets": True}
    )
    restored = ClusterConfig.model_validate_json(encoded)

    assert restored.files() == configuration.files()
    assert restored.model_fields_set == configuration.model_fields_set
    assert restored.settings.model_fields_set == configuration.settings.model_fields_set
    assert "json-key" not in configuration.model_dump_json()
    assert "json-token" not in configuration.model_dump_json()


@pytest.mark.parametrize(
    ("location", "task_set", "saved_extra"),
    [
        ("forest", "default", 'islands="always"'),
        ("cave", "cave_default", 'roads="never"'),
    ],
)
def test_loaded_level_json_preserves_game_and_custom_fields(
    tmp_path: Path,
    location: str,
    task_set: str,
    saved_extra: str,
) -> None:
    path = tmp_path / "leveldataoverride.lua"
    path.write_text(
        f'return {{id="SAVED",name="world",desc="",location="{location}",'
        f'overrides={{task_set="{task_set}",{saved_extra},custom_density=0.25}}}}',
        encoding="utf-8",
    )
    level = LevelDataOverride.load(path)
    restored = LevelDataOverride.model_validate_json(
        level.model_dump_json(exclude_unset=True)
    )

    assert type(restored.overrides) is type(level.overrides)
    assert restored.overrides.model_fields_set == level.overrides.model_fields_set
    assert restored.render() == level.render()


def test_world_json_rejects_unknown_type_tag() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        WorldgenOverride.model_validate_json(
            '{"overrides":{"kind":"unknown","values":{}}}'
        )
