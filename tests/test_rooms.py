import json
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from dst_server.configuration import files
from dst_server.configuration.models import ClusterConfig
from dst_server.configuration.world import ForestOverrides
from dst_server.deployment import QuadletApplication
from dst_server.presets.lst import build_template, fleet_room, template_names
from dst_server.rooms import (
    CONTROL_FILE,
    Control,
    DailyWindow,
    Room,
    RoomStore,
    read_control,
    write_control,
)


@pytest.mark.parametrize("template", template_names())
def test_templates_round_trip_through_native_room_files(
    tmp_path: Path,
    template: str,
) -> None:
    store = RoomStore(tmp_path)
    room = Room(
        number=299,
        template=template,
        cluster=build_template(template, number=299, token=SecretStr("test-token")),
    )

    written = store.save(room)
    loaded = store.load(299)

    assert store.path(299) / "cluster.ini" in written
    assert all(path.is_file() for path in written)
    assert loaded.template == template
    assert loaded.cluster == ClusterConfig.load(store.path(299))
    assert loaded.cluster.settings.game_mode == room.cluster.settings.game_mode
    assert loaded.cluster.shards.keys() == room.cluster.shards.keys()
    assert loaded.cluster.settings.cluster_key is not None
    assert loaded.cluster.token.get_secret_value() == "test-token"
    assert "test-token" not in loaded.model_dump_json()
    policy = json.loads((store.path(299) / CONTROL_FILE).read_text())
    assert not {"cluster", "deployment", "token"}.intersection(policy)
    assert store.numbers() == (299,)


def test_existing_native_room_needs_no_metadata_and_reads_external_edits(
    tmp_path: Path,
) -> None:
    store = RoomStore(tmp_path)
    room = Room(
        number=250,
        cluster=build_template("pure_survival", token=SecretStr("test-token")),
    )
    room.save_game(store.path(250))
    settings_path = store.path(250) / "cluster.ini"
    settings_path.write_text(
        settings_path.read_text().replace("max_players = 9", "max_players = 12")
    )
    world_path = store.path(250) / "forest/worldgenoverride.lua"
    world_path.write_text(
        "return {override_enabled=true, worldgen_preset='SURVIVAL_TOGETHER', "
        "settings_preset='SURVIVAL_TOGETHER', overrides={day='onlynight'}}"
    )

    loaded = store.load(250)

    assert loaded.cluster.settings.max_players == 12
    world = loaded.cluster.shards["forest"].world
    assert world is not None
    assert isinstance(world.overrides, ForestOverrides)
    assert world.overrides.day == "onlynight"
    assert loaded.template is None
    assert loaded.schedule == ()
    assert not loaded.recycle
    assert store.list() == (loaded,)
    assert not (store.path(250) / CONTROL_FILE).exists()


def test_changing_players_only_writes_cluster_ini(tmp_path: Path) -> None:
    store = RoomStore(tmp_path)
    store.save(fleet_room(0, token=SecretStr("test-token")))
    directory = store.path(0)
    for relative in ("forest/worldgenoverride.lua", "forest/modoverrides.lua"):
        path = directory / relative
        path.write_text("-- Keep this native comment.\n" + path.read_text())
    permissions = directory / "blocklist.txt"
    permissions.write_bytes(b"KU_newlybanned\r\n")
    world = directory / "cave/save/world"
    world.parent.mkdir()
    world.write_bytes(b"existing world")
    before = {
        path: path.read_bytes() for path in directory.rglob("*") if path.is_file()
    }
    previous = store.load(0)
    changed = previous.edit("/cluster/settings/max_players", 12)

    written = changed.save_game(directory, previous=previous)

    assert written == (directory / "cluster.ini",)
    assert store.load(0).cluster.settings.max_players == 12
    assert {
        path: path.read_bytes() for path in before if path.name != "cluster.ini"
    } == {
        path: content for path, content in before.items() if path.name != "cluster.ini"
    }
    assert (
        store.load(0).cluster.settings.cluster_key
        == previous.cluster.settings.cluster_key
    )


def test_stopping_a_shard_only_removes_its_server_ini(tmp_path: Path) -> None:
    store = RoomStore(tmp_path)
    store.save(fleet_room(0, token=SecretStr("test-token")))
    directory = store.path(0)
    world = directory / "cave/save/world"
    world.parent.mkdir()
    world.write_bytes(b"existing cave world")
    previous = store.load(0)
    retained = {
        path: path.read_bytes()
        for path in (directory / "cave").rglob("*")
        if path.is_file() and path.name != "server.ini"
    }

    previous.edit("/cluster/shards/cave", unset=True).save_game(
        directory, previous=previous
    )

    assert not (directory / "cave/server.ini").exists()
    assert tuple(store.load(0).cluster.shards) == ("forest",)
    assert {path: path.read_bytes() for path in retained} == retained


@pytest.mark.parametrize(
    ("number", "ports"),
    [(0, {30000, 30001, 30002, 30003}), (135, {31350, 31351})],
)
def test_loading_native_deployment_preserves_custom_units_and_drop_ins(
    tmp_path: Path,
    number: int,
    ports: set[int],
) -> None:
    store = RoomStore(tmp_path / "rooms", tmp_path / "quadlets")
    room = fleet_room(number, token=SecretStr("test-token")).edit_many((
        ("/deployment/image", "quay.io/example/dst:release"),
        ("/deployment/environment", {"DST_CUSTOM_SETTING": "enabled"}),
        ("/deployment/volume_idmap", "uids=0-1000-1;gids=0-1000-1"),
        ("/deployment/userns", "keep-id"),
        ("/deployment/start_on_boot", False),
    ))
    store.save(room)
    application = room.application(store.path(number))
    application.replace(master=application.master.replace(nice=5)).save(
        tmp_path / "quadlets"
    )
    override = tmp_path / "quadlets/dst-.container.d/proxy.conf"
    override.parent.mkdir()
    override.write_text(
        "[Container]\nEnvironment=DST_SERVER_MOD_PROXY=http://proxy:1080\n"
    )
    before = {
        path: path.read_bytes()
        for path in (tmp_path / "quadlets").rglob("*")
        if path.is_file()
    }

    loaded = store.load(number)

    assert loaded.deployment == room.deployment
    assert loaded.cluster.token == room.cluster.token
    assert {path: path.read_bytes() for path in before} == before
    assert QuadletApplication.load(tmp_path / "quadlets").master.nice == 5
    assert {mapping.host for mapping in application.pod.publish_ports} == ports


def test_policy_updates_preserve_control_state_and_native_files(tmp_path: Path) -> None:
    store = RoomStore(tmp_path)
    store.save(fleet_room(0, token=SecretStr("test-token")))
    directory = store.path(0)
    previous = store.load(0)
    write_control(
        directory,
        Control(
            template=previous.template,
            recycle=previous.recycle,
            paused=True,
            override=True,
            until=datetime(2030, 1, 1, tzinfo=UTC),
            revision=4,
        ),
    )
    before = (directory / "cluster.ini").read_bytes()
    changed = previous.edit_many((
        ("/template", "custom"),
        ("/schedule/-", {"start": "22:00", "end": "05:00"}),
    ))

    store.save_policy(changed)

    policy = read_control(directory)
    assert policy.template == "custom"
    assert policy.schedule == changed.schedule
    assert policy.paused
    assert policy.override is None
    assert policy.until is None
    assert policy.revision == 5
    assert (directory / "cluster.ini").read_bytes() == before
    assert store.load(0).schedule == changed.schedule


def test_pointer_edits_validate_defaults_arrays_and_escaped_fields() -> None:
    room = Room(
        number=299,
        cluster=build_template("pure_survival", token=SecretStr("test-token")),
    )
    assert room.get("/deployment/image").endswith(":latest")
    changed = room.edit("/deployment/image", "quay.io/example/dst:beta")
    changed = changed.edit("/schedule/-", {"start": "22:00", "end": "05:00"})
    assert changed.schedule == (DailyWindow(start=time(22), end=time(5)),)
    assert changed.edit("/schedule/0", unset=True).schedule == ()
    assert changed.edit("/deployment/image", unset=True).deployment.image.endswith(
        ":latest"
    )
    with pytest.raises(ValidationError):
        changed.edit("/cluster/settings/max_players", 65)
    with pytest.raises(ValueError, match="permission lists"):
        changed.edit("/cluster/blocklist", "KU_banned\n")
    with pytest.raises(ValueError, match="identity"):
        changed.edit("/number", 298)
    with pytest.raises(ValueError, match="invalid JSON Pointer"):
        changed.edit("/cluster/settings/~2", 1)
    with pytest.raises(IndexError):
        changed.edit("/schedule/01", unset=True)
    prefix = "/cluster/shards/forest/mods/entries/custom"
    changed = changed.edit(prefix, {"configuration_options": {"a/b~c": "old"}})
    changed = changed.edit(prefix + "/configuration_options/a~1b~0c", "new")
    assert changed.get(prefix + "/configuration_options/a~1b~0c") == "new"


def test_daily_windows_and_fleet_metadata_are_independent_of_template_number() -> None:
    window = DailyWindow(start=time(22), end=time(5))
    assert window.contains(time(22))
    assert window.contains(time(0))
    assert not window.contains(time(5))
    assert not window.contains(time(21, 59))
    with pytest.raises(ValueError, match="differ"):
        DailyWindow(start=time(0), end=time(0))
    morning = fleet_room(7, token=SecretStr("test-token"))
    assert morning.schedule == (DailyWindow(start=time(9), end=time(12)),)
    assert morning.recycle
    assert not morning.deployment.start_on_boot
    independent = Room(
        number=299,
        cluster=build_template("forge", number=299, token=SecretStr("test-token")),
    )
    assert independent.schedule == ()
    assert independent.cluster.settings.game_mode == "lavaarena"


def test_pointer_batch_validates_once_and_does_not_mutate_inputs() -> None:
    room = fleet_room(0, token=SecretStr("test-token"))
    window = {"start": "22:00", "end": "05:00"}
    changed = room.edit_many((
        ("/cluster/settings/whitelist_slots", 12),
        ("/cluster/settings/max_players", 12),
        ("/schedule/-", window),
        ("/schedule/0/end", "06:00"),
    ))

    assert (
        changed.cluster.settings.whitelist_slots
        == changed.cluster.settings.max_players
        == 12
    )
    assert changed.schedule == (DailyWindow(start=time(22), end=time(6)),)
    assert changed.cluster.token == room.cluster.token
    assert window["end"] == "05:00"
    assert room.cluster.settings.max_players == 9
    with pytest.raises(ValidationError):
        room.edit_many((("/cluster/settings/whitelist_slots", 12),))


def test_store_rejects_unsafe_room_paths(tmp_path: Path) -> None:
    store = RoomStore(tmp_path)
    store.save(fleet_room(0, token=SecretStr("test-token")))
    store.path(1).symlink_to(store.path(0), target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.load(1)
    with pytest.raises(ValidationError):
        store.path(300)
    with pytest.raises(ValidationError):
        store.path(-1)


def test_partial_write_failure_keeps_world_and_can_retry_requested_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RoomStore(tmp_path)
    store.save(fleet_room(0, token=SecretStr("test-token")))
    directory = store.path(0)
    previous = store.load(0)
    world = directory / "forest/save/world"
    world.parent.mkdir()
    world.write_bytes(b"existing world")
    requested = previous.edit_many((
        ("/cluster/settings/max_players", 12),
        ("/cluster/shards/forest/world/worldgen_preset", "ENDLESS"),
    ))
    write = files.atomic_write

    def fail(path: Path, content: str, mode: int) -> None:
        if path.name == "worldgenoverride.lua":
            msg = "injected game file failure"
            raise OSError(msg)
        write(path, content, mode)

    with monkeypatch.context() as patch:
        patch.setattr(files, "atomic_write", fail)
        with pytest.raises(OSError, match="game file failure"):
            requested.save_game(directory, previous=previous)
    assert world.read_bytes() == b"existing world"
    requested.save_game(directory, previous=previous)
    loaded = store.load(0)
    assert loaded.cluster.settings.max_players == 12
    assert loaded.get("/cluster/shards/forest/world/worldgen_preset") == "ENDLESS"
    assert world.read_bytes() == b"existing world"


def test_readding_shard_does_not_reactivate_its_old_overrides(tmp_path: Path) -> None:
    store = RoomStore(tmp_path)
    store.save(fleet_room(0, token=SecretStr("test-token")))
    directory = store.path(0)
    world = directory / "cave/save/world"
    world.parent.mkdir()
    world.write_bytes(b"existing cave world")
    original = store.load(0)
    store.save(original.edit("/cluster/shards/cave", unset=True))
    assert (directory / "cave/worldgenoverride.lua").exists()
    cave = original.cluster.shards["cave"].replace(world=None)
    current = store.load(0)
    store.save(
        current.replace(
            cluster=current.cluster.replace(
                shards={**current.cluster.shards, "cave": cave}
            )
        )
    )
    assert store.load(0).cluster.shards["cave"].world is None
    assert not (directory / "cave/worldgenoverride.lua").exists()
    assert world.read_bytes() == b"existing cave world"


@pytest.mark.parametrize("use_previous", [False, True])
def test_regeneration_preserves_key_stored_only_in_server_ini(
    tmp_path: Path, *, use_previous: bool
) -> None:
    store = RoomStore(tmp_path)
    key = SecretStr("existing-shard-key")
    cluster = build_template("pure_survival", token=SecretStr("test-token"))
    cluster = cluster.replace(
        shards={
            name: shard.replace(settings=shard.settings.replace(cluster_key=key))
            for name, shard in cluster.shards.items()
        }
    )
    store.save(Room(number=0, cluster=cluster))
    previous = store.load(0)
    assert previous.cluster.settings.cluster_key is None
    regenerated = previous.replace(
        cluster=build_template("pure_survival", token=previous.cluster.token)
    )
    regenerated.save_game(store.path(0), previous=previous if use_previous else None)
    loaded = store.load(0)
    assert loaded.cluster.settings.cluster_key == key
    assert all(
        (shard.settings.cluster_key or loaded.cluster.settings.cluster_key) == key
        for shard in loaded.cluster.shards.values()
    )
