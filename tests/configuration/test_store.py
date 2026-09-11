from pathlib import Path

import pytest
from pydantic import SecretStr
from ulid import ULID

from dst_server.configuration.files import Shard
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.store import ConfigurationStore
from dst_server.errors import TopologyChangeError
from dst_server.models.cluster import ConfigurationSnapshot, InvalidConfiguration


def make_cluster() -> ClusterConfig:
    return ClusterConfig(
        settings=ClusterSettings(
            cluster_key=SecretStr("cluster-key"),
            master_ip="127.0.0.1",
        ),
        shards={
            "Master": ShardConfig(
                settings=ShardSettings(
                    is_master=True,
                    server_port=10999,
                    master_server_port=27016,
                )
            ),
            "Caves": ShardConfig(
                settings=ShardSettings(
                    is_master=False,
                    name="Caves",
                    server_port=11000,
                    master_server_port=27017,
                )
            ),
        },
        token=SecretStr("cluster-token"),
    )


@pytest.fixture
def configured_store(tmp_path: Path) -> ConfigurationStore:
    make_cluster().save(tmp_path)
    return ConfigurationStore(tmp_path)


async def snapshot(store: ConfigurationStore) -> ConfigurationSnapshot:
    result = await store.read()
    assert isinstance(result, ConfigurationSnapshot)
    assert isinstance(result.revision, ULID)
    return result


async def test_read_tracks_native_configuration_changes(
    tmp_path: Path,
    configured_store: ConfigurationStore,
) -> None:
    store = configured_store
    first = await snapshot(store)
    assert store.shards == (Shard("Master", True), Shard("Caves", False))
    assert (await snapshot(store)).revision == first.revision

    settings = first.configuration.settings.replace(max_players=12)
    (tmp_path / "cluster.ini").write_text(settings.render())
    changed = await snapshot(store)

    assert changed.configuration.settings.max_players == 12
    assert changed.revision != first.revision
    assert (await snapshot(store)).revision == changed.revision
    assert (await snapshot(ConfigurationStore(tmp_path))).revision != changed.revision


async def test_invalid_native_configuration_is_read_only_and_recoverable(
    tmp_path: Path,
    configured_store: ConfigurationStore,
) -> None:
    path = tmp_path / "cluster.ini"
    original = path.read_bytes()
    path.write_text("[GAMEPLAY]\nmax_players = secret-invalid-value\n")

    invalid = await configured_store.read()

    assert isinstance(invalid, InvalidConfiguration)
    assert invalid.fields == (("max_players",),)
    assert "secret-invalid-value" not in repr(invalid)
    assert path.read_text() == "[GAMEPLAY]\nmax_players = secret-invalid-value\n"
    path.write_bytes(original)
    assert (await snapshot(configured_store)).configuration.settings.max_players == 16


@pytest.mark.parametrize(
    "source",
    [
        'local value = "SURVIVAL_TOGETHER"; return {preset = value}',
        "return {",
    ],
)
async def test_startup_topology_does_not_parse_world_configuration(
    tmp_path: Path,
    source: str,
) -> None:
    make_cluster().save(tmp_path)
    path = tmp_path / "Master/worldgenoverride.lua"
    path.write_text(source)
    store = ConfigurationStore(tmp_path)

    store.validate_deployment()

    assert isinstance(await store.read(), InvalidConfiguration)
    assert path.read_text() == source


@pytest.mark.parametrize("field", ["is_master", "master_server_port", "server_port"])
def test_store_rejects_invalid_cold_deployment_fields(
    tmp_path: Path,
    field: str,
) -> None:
    make_cluster().save(tmp_path)
    path = tmp_path / "Caves/server.ini"
    content = path.read_text()
    line = next(value for value in content.splitlines() if value.startswith(field))
    path.write_text(content.replace(line, f"{field} = invalid"))

    with pytest.raises(ValueError, match=rf"{field}|server.ini"):
        ConfigurationStore(tmp_path)


def test_store_rejects_duplicate_cold_deployment_ports(tmp_path: Path) -> None:
    make_cluster().save(tmp_path)
    path = tmp_path / "Caves/server.ini"
    path.write_text(
        path.read_text().replace("server_port = 11000", "server_port = 27016")
    )

    with pytest.raises(ValueError, match=r"UDP port 27016 .* Master.* Caves"):
        ConfigurationStore(tmp_path)


@pytest.mark.parametrize(
    ("relative", "before", "after", "fields"),
    [
        (
            "Caves/server.ini",
            "server_port = 11000",
            "server_port = 11001",
            (("shards", "Caves", "settings", "server_port"),),
        ),
        (
            "cluster.ini",
            "master_ip = 127.0.0.1",
            "master_ip = 127.0.0.2",
            (("settings", "master_ip"),),
        ),
    ],
)
def test_topology_changes_require_controller_restart(
    tmp_path: Path,
    configured_store: ConfigurationStore,
    relative: str,
    before: str,
    after: str,
    fields: tuple[tuple[str, ...], ...],
) -> None:
    path = tmp_path / relative
    path.write_text(path.read_text().replace(before, after))

    with pytest.raises(TopologyChangeError) as caught:
        configured_store.validate_deployment()

    assert caught.value.paths == fields
    ConfigurationStore(tmp_path).validate_deployment()


def test_removed_shard_keeps_world_but_requires_controller_restart(
    tmp_path: Path,
    configured_store: ConfigurationStore,
) -> None:
    directory = tmp_path / "Caves"
    world = directory / "save/world"
    world.parent.mkdir()
    world.write_bytes(b"retained world")
    (directory / "server.ini").unlink()

    with pytest.raises(TopologyChangeError) as caught:
        configured_store.validate_deployment()

    assert caught.value.paths == (("shards",),)
    assert ConfigurationStore(tmp_path).shards == (Shard("Master", True),)
    assert (tmp_path / "Caves/save/world").read_bytes() == b"retained world"
