"""Run with the isolated native wheel interpreter."""

import json
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

from dst_server.settings import (
    ClusterConfig,
    ClusterSettings,
    Configuration,
    ForestOverrides,
    ModOverrides,
    Room,
    RoomStore,
    ShardConfig,
    ShardSettings,
    WorldgenOverride,
    build_bundle,
    build_template,
    compose,
    configuration_schema,
    fleet_room,
    load_configuration,
    parse_component,
    parse_configuration,
    parse_lua_literal,
    preset,
    render_lua_literal,
    room_numbers,
    template_names,
    verify_bundle,
)

DUMMY_TOKEN = "private-token"  # ruff: ignore[hardcoded-password-string]


def configuration() -> Configuration:
    settings = ClusterSettings(cluster_key="private-key", max_players=12)
    world = WorldgenOverride(
        worldgen_preset="SURVIVAL_TOGETHER",
        overrides=ForestOverrides(day="onlynight"),
    )
    cluster = ClusterConfig(
        settings=settings,
        token=DUMMY_TOKEN,
        shards={"Surface": ShardConfig(settings=ShardSettings(), world=world)},
    )
    assert "private" not in repr(cluster)
    assert cluster.dump()["token"] == "*" * 10
    assert cluster.dump(secrets=True)["token"] == DUMMY_TOKEN
    assert (
        cluster.dump(defaults=True)["shards"]["Surface"]["settings"]["encode_user_path"]
        is True
    )
    assert settings.replace(cluster_name=None).dump()["cluster_name"] is None
    assert "cluster_name" not in settings.dump()
    for invalid in [False, 1.0, 2**80]:
        try:
            ClusterSettings(max_players=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError
    huge = 2**64 - 1
    assert ClusterSettings(steam_group_id=huge).dump()["steam_group_id"] == huge
    assert parse_configuration("WorkshopDownloads", f'ServerModSetup("{huge}")').dump()[
        "items"
    ] == [huge]
    literal = {"text": "世界\x00", "float": 1.0, "list": [False, 3]}
    parsed = parse_lua_literal(render_lua_literal(literal))
    assert parsed == literal
    assert isinstance(parsed["float"], float)
    try:
        ModOverrides(entries={"local": {"configuration_options": {"number": 2**53}}})
    except ValueError:
        pass
    else:
        raise AssertionError
    return cluster


def presets() -> None:
    assert len(template_names()) == 12
    assert room_numbers() == [*range(100), *range(200, 220)]
    built = compose([preset("FOREST_CAVES"), preset("ENDLESS")]).build(
        token=DUMMY_TOKEN
    )
    assert built.dump()["shards"]["forest"]["world"]["settings_preset"] == "ENDLESS"
    schema = configuration_schema("Room")
    assert "policy" in schema["properties"]
    assert "schedule" not in schema["properties"]
    assert "ports" in schema["$defs"]["RoomDeployment"]["properties"]


def rooms(cluster: Configuration, root: Path) -> None:
    room = Room(number=250, cluster=cluster)
    store = RoomStore(root)
    assert store.save(room)
    assert store.numbers() == [250]
    assert store.load(250).cluster.dump(secrets=True)["token"] == DUMMY_TOKEN
    assert load_configuration(store.path(250)).files() == cluster.files()
    permissions = store.path(250) / "blocklist.txt"
    permissions.write_bytes(b"KU_NEW\r\n")
    edited = store.load(250).edit("/cluster/settings/max_players", 10)
    assert store.save(edited) == [store.path(250) / "cluster.ini"]
    assert permissions.read_bytes() == b"KU_NEW\r\n"
    assert "blocklist" not in store.load(250).dump()["cluster"]
    for number, name in enumerate(template_names()):
        instance = Room(number=number, template=name, cluster=build_template(name))
        store.save(instance)
        assert store.load(number).dump()["template"] == name
    assert fleet_room(219).number == 219
    try:
        fleet_room(True)
    except ValueError:
        pass
    else:
        raise AssertionError
    control = json.loads((store.path(250) / ".dst-control.json").read_text())
    assert "policy" in control
    assert "cluster" not in control


def tools(root: Path) -> None:
    archive = root / "source.zip"
    output = root / "managed.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("scripts/main.lua", 'require("globalvariableoverrides")\n')
        bundle.writestr(
            "scripts/globalvariableoverrides.lua", "-- Intentionally blank\n"
        )
    bundle = build_bundle(archive, output)
    assert (
        verify_bundle(output, source=archive)["source_digest"]
        == bundle["source_digest"]
    )
    fields, methods = parse_component(
        "self.value = 1\nfunction Widget:Hello() return 'hello' end",
        "widget",
        "Widget",
        "components",
    )
    assert fields == ["---@field value number"]
    assert "---@return string" in methods[0]


def main() -> None:
    cluster = configuration()
    presets()
    with tempfile.TemporaryDirectory(prefix="dst-settings-") as temporary:
        root = Path(temporary)
        rooms(cluster, root)
        tools(root)
    sys.stdout.write(
        "Native Python configuration, presets, room storage and local tools passed\n"
    )


if __name__ == "__main__":
    main()
