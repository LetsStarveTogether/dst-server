from pathlib import Path

import pytest
from pydantic import SecretStr

from dst_server.configuration.files import Shard
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.store import ConfigurationStore


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


async def test_read_returns_plain_native_configuration(
    tmp_path: Path,
    configured_store: ConfigurationStore,
) -> None:
    first = await configured_store.read()
    assert configured_store.shards == (Shard("Master", True), Shard("Caves", False))
    settings = first.settings.replace(max_players=12)
    (tmp_path / "cluster.ini").write_text(settings.render())
    assert (await configured_store.read()).settings.max_players == 12


async def test_invalid_configuration_is_not_rewritten(
    tmp_path: Path,
    configured_store: ConfigurationStore,
) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text("[GAMEPLAY]\nmax_players = invalid\n")
    with pytest.raises(ValueError, match="max_players"):
        await configured_store.read()
    assert path.read_text() == "[GAMEPLAY]\nmax_players = invalid\n"


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

    with pytest.raises(ValueError, match="world override configuration"):
        await store.read()
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
