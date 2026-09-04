import sys
from pathlib import Path
from typing import cast

import pytest
from luaparser.astnodes import Node, Number, Return, Table
from pydantic import SecretStr, ValidationError

sys.path.insert(0, str(Path(__file__).parents[1]))

from dst_server.cluster.config import ClusterConfig, ClusterSettings, ShardSettings
from dst_server.cluster.overrides import (
    LuaValue,
    ModOverride,
    ModOverrides,
    _lua_statements,
    _lua_value_node,
)
from dst_server.cluster.quadlet import (
    DEFAULT_IMAGE,
    QuadletApplication,
)
from dst_server.cluster.world import LavaArenaOverrides, QuagmireOverrides
from scripts.generate_rooms import (
    CLUSTER_DESCRIPTION,
    NETDATA_ENVIRONMENT,
    ROOM_NUMBERS,
    ROOMS,
    TOKEN_ENVIRONMENT,
    RoomType,
    build,
    generate_configured_rooms,
    generate_room,
    generate_rooms,
    main,
    room,
)
from scripts.mod_configurations import MOD_CONFIGURATIONS

TOKEN = SecretStr("template-test-token")
CLUSTER_KEY = SecretStr("template-test-cluster-key")

TEMPLATES = (
    (0, 0, {"forest", "cave"}, frozenset()),
    (20, 10, {"forest", "cave"}, frozenset()),
    (
        50,
        55,
        {"forest", "cave"},
        frozenset({1803285852, 2189004162, 2950657933, 3223103565}),
    ),
    (
        70,
        55,
        {"forest", "cave"},
        frozenset({1803285852, 2189004162, 2950657933, 3223103565}),
    ),
    (100, 90, {"afk"}, frozenset({1981709850})),
    (110, 26, {"forest", "cave"}, frozenset()),
    (115, 26, {"forest", "cave"}, frozenset()),
    (
        120,
        20,
        {"shipwrecked", "volcano"},
        frozenset({1467214795, 3435352667}),
    ),
    (125, 23, {"hamlet"}, frozenset({3322803908})),
    (
        130,
        29,
        {"adventure"},
        frozenset({756229217, 1847959350}),
    ),
    (133, 30, {"gorge"}, frozenset({1918927570})),
    (
        135,
        33,
        {"xforge"},
        frozenset({1938752683, 2038128735, 2633870801, 2961923603}),
    ),
)


def _historical_mod_overrides(path: Path) -> dict[str, LuaValue]:
    statements = _lua_statements(path, "historical Mod override configuration")
    assert len(statements) == 1
    statement = statements[0]
    assert isinstance(statement, Return)
    assert len(statement.values) == 1
    root = statement.values[0]
    assert isinstance(root, Table)

    def number_implicit_arrays(node: Node) -> None:
        if not isinstance(node, Table):
            return
        implicit = [field for field in node.fields if field.key is None]
        if implicit:
            assert len(implicit) == len(node.fields)
            for index, field in enumerate(implicit, 1):
                field.key = Number(index)
        for field in node.fields:
            number_implicit_arrays(field.value)

    number_implicit_arrays(root)
    return cast(
        dict[str, LuaValue],
        _lua_value_node(root, "historical Mod override configuration"),
    )


def _typed(value: object) -> object:
    if isinstance(value, dict):
        return {key: _typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [(_typed(item), type(item)) for item in value]
    return value, type(value)


def _historical_rooms() -> Path:
    root = Path(__file__).parents[1] / "ref" / "dev"
    if not root.is_dir():
        root = root.with_name("dst")
    if not root.is_dir():
        pytest.skip("historical room fixtures are not part of the repository")
    return root


def test_room_plan_exactly_covers_the_requested_fleet() -> None:
    expanded = [number for numbers, _, _, _ in ROOMS for number in numbers]

    assert tuple(expanded) == ROOM_NUMBERS == tuple(range(140))
    assert len(set(expanded)) == len(expanded)
    assert len(ROOMS) == len(RoomType) == 12


def test_netdata_environment_is_logs_only() -> None:
    assert dict(NETDATA_ENVIRONMENT) == {
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://10.255.255.254:4317",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "none",
    }


@pytest.mark.parametrize(("number", "legacy_number", "shards", "downloads"), TEMPLATES)
def test_templates_preserve_historical_settings_and_topology(
    number: int,
    legacy_number: int,
    shards: set[str],
    downloads: frozenset[int],
) -> None:
    root = _historical_rooms()
    kind, label, max_players = room(number)
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)
    legacy = ClusterSettings.load(root / str(legacy_number) / "cluster.ini")
    legacy_shards = {
        path.parent.name: ShardSettings.load(path)
        for path in (root / str(legacy_number)).glob("*/server.ini")
    }

    assert cluster.settings.cluster_name == f"LST-{number:03d}-{label}"
    assert cluster.settings.cluster_description == legacy.cluster_description
    assert cluster.settings.cluster_description == CLUSTER_DESCRIPTION
    assert cluster.settings.max_snapshots == legacy.max_snapshots
    assert cluster.settings.steam_group_id == legacy.steam_group_id == 45_524_458
    assert cluster.settings.max_players == max_players
    assert cluster.settings.master_port == 10888
    if kind not in {RoomType.GORGE, RoomType.FORGE}:
        assert cluster.settings.game_mode == "survival"
    for field in (
        "autosaver_enabled",
        "cluster_language",
        "lan_only_cluster",
        "offline_cluster",
        "pause_when_empty",
        "pvp",
        "shard_enabled",
        "tick_rate",
        "vote_enabled",
    ):
        assert getattr(cluster.settings, field) == getattr(legacy, field)
    assert set(cluster.shards) == set(legacy_shards) == shards
    assert cluster.resolved_downloads().items == downloads

    for shard_name, shard in cluster.shards.items():
        settings = shard.settings
        assert settings.is_master == legacy_shards[shard_name].is_master
        assert (settings.server_port, settings.master_server_port) == (
            (10999, 27016) if settings.is_master else (11000, 27017)
        )


@pytest.mark.parametrize(("number", "legacy_number", "shards", "downloads"), TEMPLATES)
def test_historical_mod_options_match_generated_script(
    tmp_path: Path,
    number: int,
    legacy_number: int,
    shards: set[str],
    downloads: frozenset[int],
) -> None:
    root = _historical_rooms()
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)
    assert cluster.resolved_downloads().items == downloads
    cluster.save(tmp_path)

    for shard_name in shards:
        generated = ModOverrides.load(tmp_path / shard_name / "modoverrides.lua")
        historical = _historical_mod_overrides(
            root / str(legacy_number) / shard_name / "modoverrides.lua"
        )
        assert set(generated.entries) == set(historical)
        for mod_name, generated_override in generated.entries.items():
            raw_override = cast(dict[str, LuaValue], historical[mod_name])
            assert generated_override.enabled is raw_override["enabled"]
            raw_options = cast(
                dict[str, LuaValue],
                raw_override.get("configuration_options", {}),
            )
            configured = generated_override.configuration_options
            actual = {} if configured is None else dict(configured)
            assert _typed(actual) == _typed(raw_options)


@pytest.mark.parametrize(("number", "_", "shards", "downloads"), TEMPLATES)
def test_room_configuration_round_trip(
    tmp_path: Path,
    number: int,
    _: int,
    shards: set[str],
    downloads: frozenset[int],
) -> None:
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)
    kind, label, max_players = room(number)

    assert set(cluster.shards) == shards
    assert cluster.resolved_downloads().items == downloads
    assert cluster.settings.cluster_name == f"LST-{number:03d}-{label}"
    assert cluster.settings.max_players == max_players
    assert {"max_snapshots", "steam_group_id"}.issubset(
        cluster.settings.model_fields_set
    )
    assert ("tick_rate" in cluster.settings.model_fields_set) is (
        kind in {RoomType.FORGE, RoomType.AFK}
    )

    written = cluster.save(tmp_path)

    assert written
    level_shards = {
        path.parent.name for path in tmp_path.rglob("leveldataoverride.lua")
    }
    assert level_shards == (
        {"gorge"} if number == 133 else {"xforge"} if number == 135 else set()
    )
    assert len(tuple(tmp_path.rglob("worldgenoverride.lua"))) == len(shards)
    assert all(
        "authentication_port" not in path.read_text(encoding="utf-8")
        for path in tmp_path.glob("*/server.ini")
    )
    assert 'ServerModSetup("")' not in (
        tmp_path / "mods" / "dedicated_server_mods_setup.lua"
    ).read_text(encoding="utf-8")
    world_types = {
        name: type(shard.world.overrides)
        for name, shard in cluster.shards.items()
        if shard.world is not None
    }
    assert (
        ClusterConfig.load(
            tmp_path,
            world_overrides_types=world_types,
        ).files()
        == cluster.files()
    )


def test_every_historical_mod_has_explicit_configuration() -> None:
    expected_ids = frozenset().union(*(template[3] for template in TEMPLATES))

    assert set(MOD_CONFIGURATIONS) == expected_ids
    assert MOD_CONFIGURATIONS[1467214795]["游戏功能"] is False
    assert MOD_CONFIGURATIONS[3223103565]["SIB"] is False
    assert MOD_CONFIGURATIONS[3223103565]["SSB"] is False


def test_pure_survival_does_not_copy_room_nine_temporary_mod() -> None:
    cluster = build(9, token=TOKEN, cluster_key=CLUSTER_KEY)

    assert cluster.resolved_downloads().items == frozenset()
    assert all(not shard.mods.entries for shard in cluster.shards.values())


def test_world_templates_keep_only_real_overrides() -> None:
    pure_survival = build(0, token=TOKEN, cluster_key=CLUSTER_KEY)
    assert all(
        not shard.world.overrides.model_fields_set
        for shard in pure_survival.shards.values()
        if shard.world is not None
    )

    endless = build(20, token=TOKEN, cluster_key=CLUSTER_KEY)
    endless_forest = endless.shards["forest"].world
    endless_cave = endless.shards["cave"].world
    assert endless.settings.game_mode == "survival"
    assert endless_forest is not None
    assert endless_cave is not None
    assert endless_forest.worldgen_preset == "ENDLESS"
    assert endless_forest.settings_preset == "ENDLESS"
    assert not endless_forest.overrides.model_fields_set
    assert endless_cave.overrides.model_dump(exclude_unset=True) == {
        "basicresource_regrowth": "always",
        "ghostsanitydrain": "none",
        "portalresurection": "always",
        "resettime": "none",
    }

    semi_forest = build(50, token=TOKEN, cluster_key=CLUSTER_KEY).shards["forest"].world
    semi_cave = build(50, token=TOKEN, cluster_key=CLUSTER_KEY).shards["cave"].world
    assert semi_forest is not None
    assert semi_cave is not None
    assert semi_forest.overrides.model_dump(exclude_unset=True) == {
        "antliontribute": "never",
        "beefalo": "often",
        "boons": "often",
        "cactus": "often",
        "grass": "often",
        "grassgekkos": "never",
        "krampus": "always",
        "moon_starfish": "often",
        "ocean_bullkelp": "often",
        "prefabswaps_start": "classic",
        "reeds": "often",
        "resettime": "none",
        "sapling": "often",
        "tallbirds": "often",
        "touchstone": "often",
        "wildfires": "never",
    }
    assert semi_cave.overrides.model_dump(exclude_unset=True) == {
        "boons": "often",
        "grass": "often",
        "grassgekkos": "never",
        "krampus": "always",
        "prefabswaps_start": "classic",
        "reeds": "often",
        "resettime": "none",
        "sapling": "often",
        "touchstone": "often",
    }
    semi_endless_cave = (
        build(70, token=TOKEN, cluster_key=CLUSTER_KEY).shards["cave"].world
    )
    assert semi_endless_cave is not None
    assert semi_endless_cave.overrides.model_dump(exclude_unset=True) == {
        **semi_cave.overrides.model_dump(exclude_unset=True),
        "basicresource_regrowth": "always",
        "ghostsanitydrain": "none",
        "portalresurection": "always",
    }

    lights_out = build(115, token=TOKEN, cluster_key=CLUSTER_KEY)
    forest = lights_out.shards["forest"].world
    cave = lights_out.shards["cave"].world
    assert forest is not None
    assert cave is not None
    assert forest.worldgen_preset == "LIGHTS_OUT"
    assert forest.settings_preset == "ENDLESS"
    assert forest.overrides.model_fields_set == {"day"}
    assert cave.overrides.model_fields_set == {
        "basicresource_regrowth",
        "day",
        "ghostsanitydrain",
        "portalresurection",
        "resettime",
    }
    assert lights_out.settings.game_mode == "survival"

    afk = build(100, token=TOKEN)
    world = afk.shards["afk"].world
    assert world is not None
    assert world.overrides.model_fields_set == {
        "boons",
        "fruitfly",
        "has_ocean",
        "world_size",
    }


@pytest.mark.parametrize("number", [-1, 140])
def test_room_number_is_bounded(number: int) -> None:
    with pytest.raises(ValueError, match="0 through 139"):
        build(number, token=TOKEN, cluster_key=CLUSTER_KEY)


@pytest.mark.parametrize("number", [True, "0"])
def test_room_number_requires_an_integer(number: object) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        build(cast(int, number), token=TOKEN, cluster_key=CLUSTER_KEY)


def test_cluster_key_is_required_only_when_sharding_is_enabled() -> None:
    single = build(100, token=TOKEN)

    assert single.settings.cluster_key is None
    for number in (0, 130, 133):
        with pytest.raises(ValueError, match="shared cluster_key"):
            build(number, token=TOKEN)


def test_script_configuration_can_be_loaded_edited_and_saved(tmp_path: Path) -> None:
    cluster = build(100, token=TOKEN)
    shard = cluster.shards["afk"]
    mod_name = "workshop-1981709850"
    configured = cluster.replace(
        shards={
            "afk": shard.replace(
                mods=shard.mods.replace(
                    entries={
                        mod_name: ModOverride(
                            enabled=True,
                            configuration_options={"mode": "safe"},
                        )
                    }
                )
            )
        }
    )
    configured.save(tmp_path)

    loaded = ClusterConfig.load(tmp_path)
    loaded_shard = loaded.shards["afk"]
    loaded_mod = loaded_shard.mods.entries[mod_name]
    assert loaded.files() == configured.files()
    assert loaded_mod.configuration_options == {"mode": "safe"}

    loaded.replace(
        shards={
            "afk": loaded_shard.replace(
                mods=loaded_shard.mods.replace(
                    entries={
                        mod_name: loaded_mod.replace(
                            configuration_options={"mode": "fast"}
                        )
                    }
                )
            )
        }
    ).save(tmp_path)

    reloaded = ClusterConfig.load(tmp_path)
    assert reloaded.shards["afk"].mods.entries[mod_name].configuration_options == {
        "mode": "fast"
    }


@pytest.mark.parametrize(
    ("number", "mode", "tick_rate"),
    [(133, "quagmire", 15), (135, "lavaarena", 1)],
)
def test_event_rooms_preserve_their_game_mode(
    tmp_path: Path,
    number: int,
    mode: str,
    tick_rate: int,
) -> None:
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)

    assert cluster.settings.game_mode == mode
    assert cluster.settings.pause_when_empty is False
    assert cluster.settings.tick_rate == tick_rate
    shard = next(iter(cluster.shards.values()))
    assert shard.level is not None
    assert isinstance(
        shard.level.overrides,
        QuagmireOverrides if number == 133 else LavaArenaOverrides,
    )
    level_values = shard.level.overrides.model_dump()
    assert level_values["boons"] == level_values["touchstone"] == "never"
    assert level_values["task_set"] == (
        "quagmire_taskset" if number == 133 else "lavaarena_taskset"
    )
    assert level_values["start_location"] == (
        "quagmire_startlocation" if number == 133 else "lavaarena"
    )
    if number == 135:
        assert cluster.settings.autosaver_enabled is False
    cluster.save(tmp_path)
    assert ClusterConfig.load(tmp_path).files() == cluster.files()


def test_generate_room_saves_cluster_and_quadlet_application(tmp_path: Path) -> None:
    cluster_dir = tmp_path / "007"
    quadlet_dir = tmp_path / "quadlet"

    written = generate_room(
        7,
        token=TOKEN,
        cluster_key=CLUSTER_KEY,
        cluster_dir=cluster_dir,
        quadlet_dir=quadlet_dir,
        environment={"OTEL_SDK_DISABLED": "true"},
    )

    assert written
    assert all(path.is_file() for path in written)
    assert len(written) == len(set(written))
    assert ClusterConfig.load(cluster_dir).settings.cluster_name == "LST-007-纯净生存"
    application = QuadletApplication.load(quadlet_dir)
    units = (application.master, *application.secondaries)
    assert len(units) == 2
    assert all(unit.image == DEFAULT_IMAGE for unit in units)
    assert application.pod.publish_ports[0].host == 30070
    for unit in units:
        assert unit.environment["OTEL_SDK_DISABLED"] == "true"
        assert unit.environment["DST_SERVER_CLUSTER_NAME"] == "dst-007"


def test_generate_rooms_writes_the_complete_fleet(tmp_path: Path) -> None:
    cluster_root = tmp_path / "clusters"
    quadlet_dir = tmp_path / "quadlet"

    written = generate_rooms(
        ROOM_NUMBERS,
        token=TOKEN,
        cluster_root=cluster_root,
        quadlet_dir=quadlet_dir,
    )

    assert written
    assert len(written) == len(set(written))
    assert {path.name for path in cluster_root.iterdir()} == {
        f"{number:03d}" for number in ROOM_NUMBERS
    }
    assert len(tuple(quadlet_dir.glob("*.pod"))) == 140
    assert len(tuple(quadlet_dir.glob("*.container"))) == 255
    assert len(tuple(cluster_root.rglob("leveldataoverride.lua"))) == 7
    assert not tuple(quadlet_dir.glob("*.network"))
    ports = []
    shard_count = 0
    for number in ROOM_NUMBERS:
        application = QuadletApplication.load(
            quadlet_dir,
            name=f"dst-{number:03d}",
        )
        mappings = application.pod.publish_ports
        units = (application.master, *application.secondaries)
        shard_count += len(units)
        ports.extend(mapping.host for mapping in mappings)
        assert len(mappings) == 2 * len(units)
        base = 30000 + 10 * number
        assert tuple(mapping.host for mapping in mappings) == tuple(
            range(base, base + len(mappings))
        )
        assert all(mapping.protocol == "udp" for mapping in mappings)
        cluster = ClusterConfig.load(cluster_root / f"{number:03d}")
        player_ports = {mapping.container: mapping.host for mapping in mappings}
        master_name = next(
            name for name, shard in cluster.shards.items() if shard.settings.is_master
        )
        named_units = {
            master_name: application.master,
            **{unit.exec[-1]: unit for unit in application.secondaries},
        }
        history = room(number)[0] in {
            RoomType.PURE_SURVIVAL,
            RoomType.PURE_ENDLESS,
            RoomType.SEMI_SURVIVAL,
            RoomType.SEMI_ENDLESS,
            RoomType.LIGHTS_OUT_SURVIVAL,
            RoomType.LIGHTS_OUT_ENDLESS,
        }
        for shard_name, unit in named_units.items():
            shard = cluster.shards[shard_name]
            assert unit.exec[3] == str(player_ports[shard.settings.server_port])
            assert all(
                unit.environment[name] == value
                for name, value in NETDATA_ENVIRONMENT.items()
            )
            assert unit.environment.get("DST_SERVER_TELEMETRY_PROFILE") == (
                "history" if history else None
            )
    assert shard_count == 255
    assert len(ports) == len(set(ports)) == 510
    assert min(ports) == 30000
    assert max(ports) == 31391


def test_explicit_configurations_cover_remaining_port_slots(tmp_path: Path) -> None:
    template = build(0, token=TOKEN, cluster_key=CLUSTER_KEY)
    configurations = {
        number: template.replace(
            settings=template.settings.replace(cluster_name=f"explicit-{number:03d}")
        )
        for number in (140, 299)
    }

    generate_configured_rooms(
        configurations,
        cluster_root=tmp_path / "clusters",
        quadlet_dir=tmp_path / "quadlet",
    )

    for number, base in ((140, 31400), (299, 32990)):
        cluster = ClusterConfig.load(tmp_path / "clusters" / f"{number:03d}")
        assert cluster.settings.cluster_name == f"explicit-{number:03d}"
        application = QuadletApplication.load(
            tmp_path / "quadlet",
            name=f"dst-{number:03d}",
        )
        assert tuple(
            mapping.host for mapping in application.pod.publish_ports
        ) == tuple(range(base, base + 4))


def test_main_can_generate_selected_rooms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("template-test-token\n", encoding="utf-8")
    monkeypatch.setenv(TOKEN_ENVIRONMENT, "ignored-environment-token")
    cluster_root = tmp_path / "clusters"
    quadlet_dir = tmp_path / "quadlet"

    main([
        "0",
        "139",
        "--cluster-root",
        str(cluster_root),
        "--quadlet-dir",
        str(quadlet_dir),
        "--token-file",
        str(token_file),
    ])

    assert {path.name for path in cluster_root.iterdir()} == {
        "000",
        "139",
    }
    assert {path.name for path in quadlet_dir.glob("*.pod")} == {
        "dst-000.pod",
        "dst-139.pod",
    }
    generated_token = cluster_root / "000" / "cluster_token.txt"
    assert generated_token.read_text(encoding="utf-8") == "template-test-token\n"
    assert generated_token.stat().st_mode & 0o777 == 0o600


def test_main_reads_token_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT, "environment-token")
    cluster_root = tmp_path / "clusters"

    main([
        "0",
        "--cluster-root",
        str(cluster_root),
        "--quadlet-dir",
        str(tmp_path / "quadlet"),
    ])

    token = cluster_root / "000" / "cluster_token.txt"
    assert token.read_text(encoding="utf-8") == "environment-token\n"
    assert token.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("token", "from_file", "invalid"),
    [
        (None, False, False),
        ("", False, False),
        ("invalid token", False, True),
        ("\n", True, False),
    ],
    ids=("missing", "empty-environment", "invalid-environment", "empty-file"),
)
def test_main_rejects_invalid_token_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    from_file: bool,
    invalid: bool,
) -> None:
    cluster_root = tmp_path / "clusters"
    quadlet_dir = tmp_path / "quadlet"
    arguments = [
        "0",
        "--cluster-root",
        str(cluster_root),
        "--quadlet-dir",
        str(quadlet_dir),
    ]
    if from_file:
        assert token is not None
        token_file = tmp_path / "token"
        token_file.write_text(token, encoding="utf-8")
        arguments.extend(("--token-file", str(token_file)))
        monkeypatch.delenv(TOKEN_ENVIRONMENT, raising=False)
    elif token is None:
        monkeypatch.delenv(TOKEN_ENVIRONMENT, raising=False)
    else:
        monkeypatch.setenv(TOKEN_ENVIRONMENT, token)

    error = ValidationError if invalid else SystemExit
    message = "cluster tokens" if error is ValidationError else None
    with pytest.raises(error, match=message):
        main(arguments)

    assert not cluster_root.exists()
    assert not quadlet_dir.exists()


@pytest.mark.parametrize(
    ("cluster_dir", "quadlet_dir"),
    [(Path("room"), Path("/quadlet")), (Path("/room"), Path("quadlet"))],
)
def test_generate_room_requires_absolute_directories(
    cluster_dir: Path,
    quadlet_dir: Path,
) -> None:
    with pytest.raises(ValueError, match="must be an absolute path"):
        generate_room(
            0,
            token=TOKEN,
            cluster_key=CLUSTER_KEY,
            cluster_dir=cluster_dir,
            quadlet_dir=quadlet_dir,
        )
