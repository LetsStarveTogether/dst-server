import json
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr, ValidationError

sys.path.insert(0, str(Path(__file__).parents[2]))

from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
)
from dst_server.configuration.overrides import ModOverride
from dst_server.configuration.world import LavaArenaOverrides, QuagmireOverrides
from dst_server.deployment import (
    DEFAULT_IMAGE,
    QuadletApplication,
)
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
    room_name,
    room_schedule,
)
from scripts.mod_configurations import MOD_CONFIGURATIONS

TOKEN = SecretStr("template-test-token")
CLUSTER_KEY = SecretStr("template-test-cluster-key")

TEMPLATES = (
    (
        0,
        "forest",
        {
            "forest": ("SURVIVAL_TOGETHER", "SURVIVAL_TOGETHER"),
            "cave": ("DST_CAVE", "DST_CAVE"),
        },
        frozenset(),
    ),
    (
        20,
        "forest",
        {"forest": ("ENDLESS", "ENDLESS"), "cave": ("DST_CAVE", "DST_CAVE")},
        frozenset(),
    ),
    (
        50,
        "forest",
        {
            "forest": ("SURVIVAL_TOGETHER", "SURVIVAL_TOGETHER"),
            "cave": ("DST_CAVE", "DST_CAVE"),
        },
        frozenset({1803285852, 2189004162, 2950657933, 3223103565, 3046339764}),
    ),
    (
        70,
        "forest",
        {"forest": ("ENDLESS", "ENDLESS"), "cave": ("DST_CAVE", "DST_CAVE")},
        frozenset({1803285852, 2189004162, 2950657933, 3223103565, 3046339764}),
    ),
    (100, "afk", {"afk": ("ENDLESS", "ENDLESS")}, frozenset({1981709850})),
    (
        110,
        "forest",
        {"forest": ("LIGHTS_OUT", "LIGHTS_OUT"), "cave": ("DST_CAVE", "DST_CAVE")},
        frozenset(),
    ),
    (
        115,
        "forest",
        {"forest": ("LIGHTS_OUT", "ENDLESS"), "cave": ("DST_CAVE", "DST_CAVE")},
        frozenset(),
    ),
    (
        120,
        "shipwrecked",
        {
            "shipwrecked": ("SHIPWRECKED_ENDLESS", "SHIPWRECKED_ENDLESS"),
            "volcano": ("SURVIVAL_VOLCANO_CLASSIC", "SURVIVAL_VOLCANO_CLASSIC"),
        },
        frozenset({1467214795, 3435352667}),
    ),
    (
        125,
        "hamlet",
        {"hamlet": ("PORKLAND_DEFAULT", "PORKLAND_DEFAULT")},
        frozenset({3322803908}),
    ),
    (
        130,
        "adventure",
        {"adventure": ("SURVIVAL_TOGETHER", "SURVIVAL_TOGETHER")},
        frozenset({756229217, 1847959350}),
    ),
    (133, "gorge", {"gorge": ("QUAGMIRE", "QUAGMIRE")}, frozenset({1918927570})),
    (
        135,
        "xforge",
        {"xforge": ("LAVAARENA", "LAVAARENA")},
        frozenset({1938752683, 2038128735, 2633870801, 2961923603}),
    ),
)


def test_room_plan_exactly_covers_the_requested_fleet() -> None:
    expanded = [number for numbers, _, _, _ in ROOMS for number in numbers]

    assert tuple(expanded) == ROOM_NUMBERS == tuple(range(140))
    assert len(set(expanded)) == len(expanded)
    assert len(ROOMS) == len(RoomType) == 12


def test_room_schedules_have_five_equal_groups_and_complete_names() -> None:
    groups = {
        None: (range(4), range(20, 26), range(50, 54), range(70, 76)),
        ("晨餐", 9, 12): (range(4, 8), range(26, 32), range(54, 58), range(76, 82)),
        ("午膳", 13, 18): (range(8, 12), range(32, 38), range(58, 62), range(82, 88)),
        ("晚宴", 19, 0): (range(12, 16), range(38, 44), range(62, 66), range(88, 94)),
        ("夜饮", 22, 5): (range(16, 20), range(44, 50), range(66, 70), range(94, 100)),
    }
    seen = []
    promotion = " | 朗诵团 5 周年啦！入团找到你未来的 5 年好饥友吧~"  # ruff: ignore[ambiguous-unicode-character-string]
    for schedule, ranges in groups.items():
        numbers = [number for group in ranges for number in group]
        assert len(numbers) == 20
        seen.extend(numbers)
        for number in numbers:
            assert room_schedule(number) == schedule
            suffix = f"-{schedule[0]}" if schedule else ""
            assert room_name(number) == (
                f"LST-{number:03d}-{room(number)[1]}{suffix}{promotion}"
            )
    assert sorted(seen) == list(range(100))
    for number in range(100, 140):
        assert room_schedule(number) is None
        assert room_name(number) == f"LST-{number:03d}-{room(number)[1]}{promotion}"


@pytest.mark.parametrize("number", [-1, 140, True, "7"])
def test_schedule_and_name_reject_invalid_room_numbers(number: object) -> None:
    error = TypeError if isinstance(number, (bool, str)) else ValueError
    for function in (room_schedule, room_name):
        with pytest.raises(error, match="room number must be an integer"):
            function(cast("int", number))


def test_netdata_environment_is_logs_only() -> None:
    assert dict(NETDATA_ENVIRONMENT) == {
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://10.255.255.254:4317",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "none",
    }


@pytest.mark.parametrize(("number", "master", "shards", "downloads"), TEMPLATES)
def test_template_settings_worlds_and_shard_roles(
    number: int,
    master: str,
    shards: dict[str, tuple[str, str]],
    downloads: frozenset[int],
) -> None:
    _, _, max_players = room(number)
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)
    expected = {
        "cluster_name": room_name(number),
        "cluster_description": CLUSTER_DESCRIPTION,
        "max_snapshots": 9 if number == 100 else 999_999_999,
        "steam_group_id": 45_524_458,
        "max_players": max_players,
        "master_port": 10888,
        "game_mode": {133: "quagmire", 135: "lavaarena"}.get(number, "survival"),
        "autosaver_enabled": number != 135,
        "cluster_language": "zh",
        "lan_only_cluster": False,
        "offline_cluster": False,
        "pause_when_empty": number not in {133, 135},
        "pvp": False,
        "shard_enabled": number not in {100, 125, 135},
        "tick_rate": 1 if number in {100, 135} else 15,
        "vote_enabled": number != 100,
    }
    assert cluster.settings.model_dump(include=set(expected)) == expected
    assert set(cluster.shards) == set(shards)
    assert cluster.resolved_downloads().items == downloads
    assert cluster.token == TOKEN
    assert cluster.settings.cluster_key == CLUSTER_KEY
    assert cluster.settings.cluster_name is not None
    assert not cluster.settings.cluster_name.isascii()

    for name, shard in cluster.shards.items():
        assert shard.settings.is_master is (name == master)
        assert (shard.settings.server_port, shard.settings.master_server_port) == (
            (10999, 27016) if name == master else (11000, 27017)
        )
        assert shard.settings.encode_user_path is True
        assert shard.world is not None
        assert (shard.world.worldgen_preset, shard.world.settings_preset) == shards[
            name
        ]


@pytest.mark.parametrize(("number", "_master", "shards", "downloads"), TEMPLATES)
def test_template_mod_options_and_worlds_execute_in_native_lua(
    tmp_path: Path,
    luajit: str,
    number: int,
    _master: str,
    shards: dict[str, tuple[str, str]],
    downloads: frozenset[int],
) -> None:
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)
    cluster.save(tmp_path)
    expected_mods = {
        f"workshop-{identifier}": {
            "enabled": True,
            **(
                {"configuration_options": MOD_CONFIGURATIONS[identifier]}
                if MOD_CONFIGURATIONS[identifier]
                else {}
            ),
        }
        for identifier in downloads
    }
    expected_worlds = {}
    for name in shards:
        world = cluster.shards[name].world
        assert world is not None
        values = world.model_dump(exclude_unset=True, exclude_none=True)
        values["override_enabled"] = values.pop("enabled", True)
        values.setdefault("overrides", {})
        expected_worlds[name] = values
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps(
            {
                "mods": expected_mods,
                "worlds": expected_worlds,
                "downloads": [str(identifier) for identifier in sorted(downloads)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository = Path(__file__).parents[2]
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            luajit,
            str(repository / "tests/lua/room_contract.lua"),
            str(repository),
            str(tmp_path),
            str(expected),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize(("number", "_master", "shards", "downloads"), TEMPLATES)
def test_room_configuration_round_trip(
    tmp_path: Path,
    number: int,
    _master: str,
    shards: dict[str, tuple[str, str]],
    downloads: frozenset[int],
) -> None:
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)
    kind, _, max_players = room(number)

    assert set(cluster.shards) == set(shards)
    assert cluster.resolved_downloads().items == downloads
    assert cluster.settings.cluster_name == room_name(number)
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


def test_every_template_mod_has_explicit_configuration() -> None:
    expected_ids = frozenset().union(*(template[3] for template in TEMPLATES))

    assert set(MOD_CONFIGURATIONS) == expected_ids
    assert MOD_CONFIGURATIONS[1467214795]["游戏功能"] is False
    assert MOD_CONFIGURATIONS[3223103565]["SIB"] is False
    assert MOD_CONFIGURATIONS[3223103565]["SSB"] is False


@pytest.mark.parametrize("number", [50, 70])
def test_semi_rooms_enable_stacked_trade_with_workshop_defaults(number: int) -> None:
    cluster = build(number, token=TOKEN, cluster_key=CLUSTER_KEY)

    for shard in cluster.shards.values():
        override = shard.mods.entries["workshop-3046339764"]
        assert override.enabled is True
        assert override.configuration_options == {
            "antlion": True,
            "birdcage": True,
            "mermking": True,
            "monkeyisland_portal": False,
            "monkeyqueen": True,
            "pigking": True,
        }


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
    assert world.overrides.model_dump(exclude_unset=True)["has_ocean"] is True


@pytest.mark.parametrize("number", [-1, 140])
def test_room_number_is_bounded(number: int) -> None:
    with pytest.raises(ValueError, match="0 through 139"):
        build(number, token=TOKEN, cluster_key=CLUSTER_KEY)


@pytest.mark.parametrize("number", [True, "0"])
def test_room_number_requires_an_integer(number: object) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        build(cast(int, number), token=TOKEN, cluster_key=CLUSTER_KEY)


@pytest.mark.parametrize("number", [0, 100, 130, 133])
def test_cluster_key_is_generated_only_on_save(tmp_path: Path, number: int) -> None:
    cluster = build(number, token=TOKEN)

    assert cluster.settings.cluster_key is None
    assert "cluster_key" not in cluster.files()[Path("cluster.ini")]

    cluster.save(tmp_path)

    assert ClusterConfig.load(tmp_path).settings.cluster_key


def test_script_configuration_can_be_loaded_edited_and_saved(tmp_path: Path) -> None:
    cluster = build(100, token=TOKEN, cluster_key=CLUSTER_KEY)
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


def test_gorge_rooms_force_an_empty_blocklist(tmp_path: Path) -> None:
    for number in (132, 133, 134):
        directory = tmp_path / str(number)
        directory.mkdir()
        (directory / "blocklist.txt").write_text("KU_blocked\n", encoding="utf-8")
        build(number, token=TOKEN, cluster_key=CLUSTER_KEY).save(directory)

    assert (tmp_path / "132" / "blocklist.txt").read_text(encoding="utf-8") == (
        "KU_blocked\n"
    )
    assert (tmp_path / "133" / "blocklist.txt").read_bytes() == b""
    assert (tmp_path / "134" / "blocklist.txt").read_bytes() == b""


@pytest.mark.parametrize(
    ("volume_idmap", "userns"),
    [
        (None, None),
        ("uids=0-1000-1;gids=0-1000-1", None),
        (None, "keep-id:uid=1000,gid=1000"),
    ],
)
def test_generate_room_saves_cluster_and_quadlet_application(
    tmp_path: Path,
    volume_idmap: str | None,
    userns: str | None,
) -> None:
    cluster_dir = tmp_path / "007"
    quadlet_dir = tmp_path / "quadlet"

    written = generate_room(
        7,
        token=TOKEN,
        cluster_key=CLUSTER_KEY,
        cluster_dir=cluster_dir,
        quadlet_dir=quadlet_dir,
        environment={"OTEL_SDK_DISABLED": "true"},
        volume_idmap=volume_idmap,
        userns=userns,
    )

    assert written
    assert all(path.is_file() for path in written)
    assert len(written) == len(set(written))
    assert ClusterConfig.load(cluster_dir).settings.cluster_name == room_name(7)
    application = QuadletApplication.load(quadlet_dir)
    units = (application.master, *application.secondaries)
    assert len(units) == 2
    assert all(unit.image == DEFAULT_IMAGE for unit in units)
    assert application.pod.publish_ports[0].host == 30070
    assert application.pod.userns == userns
    assert application.pod.wanted_by == ()
    for unit in units:
        assert unit.pull == "always"
        assert unit.timeout_start_sec == 1800
        assert all(volume.idmap == volume_idmap for volume in unit.volumes)
        assert unit.environment["OTEL_SDK_DISABLED"] == "true"
        assert unit.environment["DST_SERVER_CLUSTER_NAME"] == "dst-007"
        assert (unit.notify, unit.watchdog_sec, unit.restart) == (
            True,
            300,
            "on-failure",
        )
        assert (unit.kill_mode, unit.watchdog_signal) == ("control-group", "SIGKILL")


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
        assert application.pod.userns is None
        assert application.pod.wanted_by == (
            ("default.target",) if room_schedule(number) is None else ()
        )
        units = (application.master, *application.secondaries)
        assert all(volume.idmap is None for unit in units for volume in unit.volumes)
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
            assert unit.image == DEFAULT_IMAGE
            assert unit.pull == "always"
            assert unit.timeout_start_sec == 1800
            assert (unit.notify, unit.watchdog_sec, unit.restart) == (
                True,
                300,
                "on-failure",
            )
            assert (unit.kill_mode, unit.watchdog_signal) == (
                "control-group",
                "SIGKILL",
            )
            assert "Health" not in unit.render()
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


def test_generate_rooms_uses_distinct_persistent_cluster_keys(tmp_path: Path) -> None:
    cluster_root = tmp_path / "clusters"
    keys = []
    for _ in range(2):
        generate_rooms(
            (0, 7, 100),
            token=TOKEN,
            cluster_root=cluster_root,
            quadlet_dir=tmp_path / "quadlet",
        )
        keys.append(
            tuple(
                ClusterSettings.load(
                    cluster_root / f"{number:03d}/cluster.ini"
                ).cluster_key
                for number in (0, 7, 100)
            )
        )
        assert ClusterSettings.load(cluster_root / "007/cluster.ini").cluster_name == (
            room_name(7)
        )

    assert all(keys[0])
    assert len(set(keys[0])) == 3
    assert keys[0] == keys[1]


@pytest.mark.parametrize(
    ("volume_idmap", "userns"),
    [
        ("uids=0-1000-1;gids=0-1000-1", None),
        (None, "keep-id:uid=1000,gid=1000"),
    ],
)
def test_explicit_configurations_cover_remaining_port_slots(
    tmp_path: Path,
    volume_idmap: str | None,
    userns: str | None,
) -> None:
    template = build(0, token=TOKEN, cluster_key=CLUSTER_KEY)
    configurations = {
        number: template.replace(
            settings=template.settings.replace(cluster_name=f"explicit-{number:03d}")
        )
        for number in (7, 140, 299)
    }

    generate_configured_rooms(
        configurations,
        cluster_root=tmp_path / "clusters",
        quadlet_dir=tmp_path / "quadlet",
        volume_idmap=volume_idmap,
        userns=userns,
    )

    for number, base in ((7, 30070), (140, 31400), (299, 32990)):
        cluster = ClusterConfig.load(tmp_path / "clusters" / f"{number:03d}")
        assert cluster.settings.cluster_name == f"explicit-{number:03d}"
        application = QuadletApplication.load(
            tmp_path / "quadlet",
            name=f"dst-{number:03d}",
        )
        assert tuple(
            mapping.host for mapping in application.pod.publish_ports
        ) == tuple(range(base, base + 4))
        assert application.pod.userns == userns
        assert application.pod.wanted_by == ("default.target",)
        assert all(
            volume.idmap == volume_idmap
            for unit in (application.master, *application.secondaries)
            for volume in unit.volumes
        )


@pytest.mark.parametrize(
    ("mapping_options", "volume_idmap", "userns"),
    [
        ((), None, None),
        (
            ("--volume-idmap", "uids=0-1000-1;gids=0-1000-1"),
            "uids=0-1000-1;gids=0-1000-1",
            None,
        ),
        (("--userns", "keep-id:uid=1000,gid=1000"), None, "keep-id:uid=1000,gid=1000"),
    ],
)
def test_main_can_generate_selected_rooms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mapping_options: tuple[str, ...],
    volume_idmap: str | None,
    userns: str | None,
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
        *mapping_options,
    ])

    assert {path.name for path in cluster_root.iterdir()} == {
        "000",
        "139",
    }
    assert {path.name for path in quadlet_dir.glob("*.pod")} == {
        "dst-000.pod",
        "dst-139.pod",
    }
    for number in (0, 139):
        application = QuadletApplication.load(quadlet_dir, name=f"dst-{number:03d}")
        assert application.pod.userns == userns
        assert all(
            volume.idmap == volume_idmap
            for unit in (application.master, *application.secondaries)
            for volume in unit.volumes
        )
    generated_token = cluster_root / "000" / "cluster_token.txt"
    assert generated_token.read_text(encoding="utf-8") == "template-test-token\n"
    assert generated_token.stat().st_mode & 0o777 == 0o600


def test_main_reads_token_from_environment_and_preserves_beta_image(
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
        "--image",
        "quay.io/wh2099/dst-server:beta",
    ])

    application = QuadletApplication.load(tmp_path / "quadlet")
    for unit in (application.master, *application.secondaries):
        assert unit.image == "quay.io/wh2099/dst-server:beta"
        assert unit.pull == "always"
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
