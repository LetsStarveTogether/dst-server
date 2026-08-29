import asyncio
import os
from pathlib import Path

import pytest
from pydantic import SecretStr
from ulid import ULID

from dst_server.cluster.config import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.cluster.configuration import (
    ConfigurationSnapshot,
    ConfigurationStore,
    ConfigurationWriteError,
    GamesRunningError,
    InvalidConfiguration,
    RevisionConflictError,
    TopologyChangeError,
)


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
def configured_store(tmp_path: Path) -> tuple[ConfigurationStore, ClusterConfig]:
    desired = make_cluster()
    desired.save(tmp_path)
    return ConfigurationStore(tmp_path), desired


async def snapshot(store: ConfigurationStore) -> ConfigurationSnapshot:
    result = await store.read()
    assert isinstance(result, ConfigurationSnapshot)
    assert str(ULID.from_str(result.revision)) == result.revision
    return result


async def test_revision_tracks_content_and_is_local_to_store(
    tmp_path: Path,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, _ = configured_store
    first = await snapshot(store)

    os.utime(tmp_path / "cluster.ini", ns=(1_000_000_000, 1_000_000_000))
    assert (await snapshot(store)).revision == first.revision

    (tmp_path / "adminlist.txt").write_text("KU_admin\n", encoding="utf-8")
    changed = await snapshot(store)
    assert changed.revision != first.revision
    assert changed.configuration.adminlist == "KU_admin\n"
    assert (await snapshot(ConfigurationStore(tmp_path))).revision != changed.revision


async def test_invalid_configuration_rotates_revision_and_can_be_repaired(
    tmp_path: Path,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, desired = configured_store
    valid = await snapshot(store)
    (tmp_path / "Caves" / "server.ini").unlink()

    invalid = await store.read()
    assert isinstance(invalid, InvalidConfiguration)
    assert invalid.revision != valid.revision

    repaired = await store.save(invalid.revision, desired, all_stopped=True)
    assert repaired.revision != invalid.revision
    assert repaired.configuration.files() == desired.files()


async def test_locked_topology_allows_repairing_non_topology_data(
    tmp_path: Path,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, desired = configured_store
    path = tmp_path / "Caves" / "server.ini"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[ACCOUNT]\nencode_user_path = invalid\n",
        encoding="utf-8",
    )

    invalid = await store.read()
    assert isinstance(invalid, InvalidConfiguration)
    repaired = await store.save(invalid.revision, desired, all_stopped=True)

    assert repaired.configuration.files() == desired.files()


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("SHARD", "is_master"),
        ("STEAM", "master_server_port"),
        ("NETWORK", "server_port"),
    ],
)
def test_store_rejects_invalid_cold_deployment_fields(
    tmp_path: Path,
    section: str,
    field: str,
) -> None:
    make_cluster().save(tmp_path)
    path = tmp_path / "Caves" / "server.ini"
    content = path.read_text(encoding="utf-8")
    line = next(value for value in content.splitlines() if value.startswith(field))
    path.write_text(content.replace(line, f"{field} = invalid"), encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{section}|{field}|server.ini"):
        ConfigurationStore(tmp_path)


def test_store_rejects_duplicate_cold_deployment_ports(tmp_path: Path) -> None:
    make_cluster().save(tmp_path)
    path = tmp_path / "Caves" / "server.ini"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "server_port = 11000",
            "server_port = 27016",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"UDP port 27016 .* Master.* Caves"):
        ConfigurationStore(tmp_path)


@pytest.mark.parametrize("all_stopped", [False, True], ids=["running", "stale"])
async def test_save_requires_stopped_games_and_current_revision(
    tmp_path: Path,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
    all_stopped: bool,
) -> None:
    store, _ = configured_store
    current = await snapshot(store)
    desired = current.configuration.replace(
        settings=current.configuration.settings.replace(max_players=12)
    )
    expected_error: type[GamesRunningError | RevisionConflictError]
    if all_stopped:
        (tmp_path / "adminlist.txt").write_text("KU_external\n", encoding="utf-8")
        expected_error = RevisionConflictError
    else:
        expected_error = GamesRunningError

    with pytest.raises(expected_error):
        await store.save(current.revision, desired, all_stopped=all_stopped)

    assert ClusterConfig.load(tmp_path).settings.max_players != 12


async def test_each_successful_save_rotates_revision(
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, _ = configured_store
    current = await snapshot(store)
    desired = current.configuration.replace(
        settings=current.configuration.settings.replace(max_players=12)
    )

    saved = await store.save(current.revision, desired, all_stopped=True)
    unchanged = await store.save(
        saved.revision,
        saved.configuration,
        all_stopped=True,
    )

    assert saved.configuration.settings.max_players == 12
    assert len({current.revision, saved.revision, unchanged.revision}) == 3


def topology_change(configuration: ClusterConfig, change: str) -> ClusterConfig:
    shards = dict(configuration.shards)
    if change == "shards":
        shards.pop("Caves")
    elif change == "master":
        shards["Master"] = shards["Master"].replace(
            settings=shards["Master"].settings.replace(
                is_master=False,
                name="Master",
            )
        )
        shards["Caves"] = shards["Caves"].replace(
            settings=shards["Caves"].settings.replace(is_master=True)
        )
    else:
        caves = shards["Caves"]
        shards["Caves"] = caves.replace(
            settings=caves.settings.replace(**{change: 11001})
        )
    return configuration.replace(shards=shards)


@pytest.mark.parametrize(
    ("change", "paths"),
    [
        ("shards", (("shards",),)),
        (
            "master",
            (
                ("shards", "Caves", "settings", "is_master"),
                ("shards", "Master", "settings", "is_master"),
            ),
        ),
        ("server_port", (("shards", "Caves", "settings", "server_port"),)),
        (
            "master_server_port",
            (("shards", "Caves", "settings", "master_server_port"),),
        ),
    ],
)
async def test_save_rejects_topology_changes(
    configured_store: tuple[ConfigurationStore, ClusterConfig],
    change: str,
    paths: tuple[tuple[str, ...], ...],
) -> None:
    store, _ = configured_store
    current = await snapshot(store)

    with pytest.raises(TopologyChangeError) as caught:
        await store.save(
            current.revision,
            topology_change(current.configuration, change),
            all_stopped=True,
        )

    assert caught.value.paths == paths
    assert (await snapshot(store)).revision == current.revision


async def test_external_topology_drift_is_locked(
    tmp_path: Path,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, _ = configured_store
    path = tmp_path / "Caves" / "server.ini"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "server_port = 11000",
            "server_port = 11001",
        ),
        encoding="utf-8",
    )

    drifted = await snapshot(store)
    with pytest.raises(TopologyChangeError) as caught:
        store.validate_deployment(drifted.configuration)

    assert caught.value.paths == (("shards", "Caves", "settings", "server_port"),)


async def test_concurrent_saves_have_one_revision_winner(
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, _ = configured_store
    current = await snapshot(store)
    desired = current.configuration.replace(
        settings=current.configuration.settings.replace(max_players=12)
    )

    results = await asyncio.gather(
        *(store.save(current.revision, desired, all_stopped=True) for _ in range(2)),
        return_exceptions=True,
    )

    assert [type(result) for result in results].count(ConfigurationSnapshot) == 1
    assert [type(result) for result in results].count(RevisionConflictError) == 1


async def test_partial_write_failure_rotates_revision_and_is_repairable(
    monkeypatch: pytest.MonkeyPatch,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, _ = configured_store
    current = await snapshot(store)
    desired = current.configuration.replace(
        settings=current.configuration.settings.replace(max_players=12)
    )
    save = ClusterConfig.save

    def fail_after_first_file(_configuration: ClusterConfig, directory: Path) -> object:
        (directory / "cluster.ini").write_text("broken", encoding="utf-8")
        msg = "injected multi-file write failure"
        raise OSError(msg)

    monkeypatch.setattr(ClusterConfig, "save", fail_after_first_file)
    with pytest.raises(ConfigurationWriteError) as caught:
        await store.save(current.revision, desired, all_stopped=True)

    assert caught.value.revision != current.revision
    assert isinstance(await store.read(), InvalidConfiguration)

    monkeypatch.setattr(ClusterConfig, "save", save)
    repaired = await store.save(caught.value.revision, desired, all_stopped=True)
    assert repaired.configuration == desired


async def test_three_consecutive_external_drifts_return_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_store: tuple[ConfigurationStore, ClusterConfig],
) -> None:
    store, _ = configured_store
    load = ClusterConfig.load
    calls = 0

    def drift(directory: Path) -> ClusterConfig:
        nonlocal calls
        loaded = load(directory)
        calls += 1
        (tmp_path / "adminlist.txt").write_text(f"KU_{calls}\n", encoding="utf-8")
        return loaded

    monkeypatch.setattr(ClusterConfig, "load", staticmethod(drift))
    result = await store.read()

    assert calls == 3
    assert isinstance(result, InvalidConfiguration)
    assert result.paths == (("configuration",),)
