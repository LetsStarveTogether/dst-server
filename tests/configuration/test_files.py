import os
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from dst_server import mods
from dst_server.configuration import files as configuration
from dst_server.configuration.files import discover
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.overrides import (
    ModOverrides,
    ModSettings,
    WorkshopDownloads,
)
from dst_server.configuration.presets import (
    FOREST_CAVES,
)
from dst_server.configuration.world import (
    CaveOverrides,
    ForestOverrides,
)
from tests.configuration.helpers import (
    make_cluster,
)


def test_cluster_cascade_saves_complete_tree_and_preserves_game_files(
    tmp_path: Path,
) -> None:
    cluster = make_cluster()
    (tmp_path / "Master" / "save").mkdir(parents=True)
    (tmp_path / "Master" / "save" / "world").write_text(
        "keep",
        encoding="utf-8",
    )
    (tmp_path / "Master" / "leveldataoverride.lua").write_text(
        'return { id = "SDK", name = "SDK", desc = "", '
        'location = "forest", overrides = { task_set = "default" } }',
        encoding="utf-8",
    )
    written = cluster.save(tmp_path)
    saved = ClusterConfig.load(tmp_path)

    assert written
    assert saved.settings.shard_enabled is True
    assert saved.shards["Master"].settings.is_master is True
    assert saved.shards["Caves"].settings.is_master is False
    assert (tmp_path / "cluster_token.txt").read_text(encoding="utf-8") == (
        "test-token\n"
    )
    assert (tmp_path / "adminlist.txt").read_text(encoding="utf-8") == "KU_admin\n"
    assert (tmp_path / "whitelist.txt").read_text(encoding="utf-8") == "KU_friend\n"
    assert (tmp_path / "blocklist.txt").read_text(encoding="utf-8") == (
        "76561198000000000\n"
    )
    assert (tmp_path / "cluster_token.txt").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "cluster.ini").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "Master" / "server.ini").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "mods" / "ugc").is_dir()
    assert saved.downloads == WorkshopDownloads(
        items=frozenset({8, 42}), collections=frozenset({99})
    )
    assert (tmp_path / "Master" / "save" / "world").read_text(
        encoding="utf-8"
    ) == "keep"
    assert (tmp_path / "Master" / "leveldataoverride.lua").read_text(
        encoding="utf-8"
    ) == (
        'return { id = "SDK", name = "SDK", desc = "", '
        'location = "forest", overrides = { task_set = "default" } }'
    )
    assert {shard.name for shard in discover(tmp_path)} == {"Master", "Caves"}
    assert all((tmp_path / name).is_file() for name in configuration.PERMISSION_FILES)

    (tmp_path / "cluster_token.txt").write_text("preserve-token\n", encoding="utf-8")
    (tmp_path / "mods" / "modsettings.lua").write_text(
        'ForceEnableMod("local-dev-mod")\n',
        encoding="utf-8",
    )
    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        downloads=cluster.downloads,
    ).save(tmp_path)
    assert (tmp_path / "cluster_token.txt").read_text(
        encoding="utf-8"
    ) == "preserve-token\n"
    assert (tmp_path / "adminlist.txt").read_text(encoding="utf-8") == "KU_admin\n"
    assert (tmp_path / "whitelist.txt").read_text(encoding="utf-8") == "KU_friend\n"
    assert (tmp_path / "blocklist.txt").read_text(encoding="utf-8") == (
        "76561198000000000\n"
    )
    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == (
        'ForceEnableMod("local-dev-mod")\n'
    )

    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        adminlist="",
        downloads=cluster.downloads,
    ).save(tmp_path)
    assert (tmp_path / "adminlist.txt").read_text(encoding="utf-8") == ""

    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        downloads=cluster.downloads,
        mod_settings=ModSettings(),
    ).save(tmp_path)
    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == ""


def test_cluster_load_edit_save_round_trip(tmp_path: Path) -> None:
    original = make_cluster()
    original.save(tmp_path)
    (tmp_path / "cluster_token.txt").write_bytes(b"test-token\r\n")
    (tmp_path / "adminlist.txt").write_bytes(b"KU_admin\r\n")
    (tmp_path / "whitelist.txt").write_bytes(b"KU_friend\r\n")
    (tmp_path / "blocklist.txt").write_bytes(b"76561198000000000\r\n")

    loaded = ClusterConfig.load(tmp_path)

    assert loaded.files() == original.files()
    master_world = loaded.shards["Master"].world
    cave_world = loaded.shards["Caves"].world
    assert master_world is not None
    assert cave_world is not None
    assert isinstance(master_world.overrides, ForestOverrides)
    assert isinstance(cave_world.overrides, CaveOverrides)

    edited = loaded.replace(
        settings=loaded.settings.replace(pvp=True),
        adminlist="",
    )
    edited.save(tmp_path)
    reloaded = ClusterConfig.load(tmp_path)

    assert reloaded.settings.pvp is True
    assert reloaded.adminlist == ""

    (tmp_path / "adminlist.txt").write_bytes(b"KU_admin\rKU_other\n")
    with pytest.raises(ValidationError, match="permission files"):
        ClusterConfig.load(tmp_path)


def test_cluster_load_rejects_missing_multi_shard_enable_flag(
    tmp_path: Path,
) -> None:
    make_cluster().save(tmp_path)
    cluster_ini = tmp_path / "cluster.ini"
    source = cluster_ini.read_text(encoding="utf-8")
    marker = "shard_enabled = true\n"
    assert marker in source
    cluster_ini.write_text(source.replace(marker, "", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="shard_enabled = true is required"):
        ClusterConfig.load(tmp_path)

    cluster_ini.write_text(
        source.replace(marker, "shard_enabled = false\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="cannot be false"):
        ClusterConfig.load(tmp_path)


def test_cluster_load_rejects_implicit_multi_shard_role(tmp_path: Path) -> None:
    make_cluster().save(tmp_path)
    server_ini = tmp_path / "Master" / "server.ini"
    source = server_ini.read_text(encoding="utf-8")
    marker = "is_master = true\n"
    assert marker in source
    server_ini.write_text(source.replace(marker, "", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="is_master is required"):
        ClusterConfig.load(tmp_path)


def test_preserved_modsettings_keep_their_existing_downloads(tmp_path: Path) -> None:
    shards = {"Master": ShardConfig(settings=ShardSettings(is_master=True))}
    ClusterConfig(
        shards=shards,
        mod_settings=ModSettings(force_enabled=frozenset({"workshop-123"})),
    ).save(tmp_path)

    ClusterConfig(shards=shards).save(tmp_path)

    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == (
        'ForceEnableMod("workshop-123")\n'
    )
    assert WorkshopDownloads.load(
        tmp_path / "mods" / "dedicated_server_mods_setup.lua"
    ) == WorkshopDownloads(items=frozenset({123}))

    ClusterConfig(shards=shards, mod_settings=ModSettings()).save(tmp_path)
    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == ""
    assert (tmp_path / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == ""


@pytest.mark.parametrize("value", ["KU_one\0KU_two", "KU_one\r\nKU_two", "\ud800"])
def test_cluster_rejects_invalid_permission_file_text(value: str) -> None:
    with pytest.raises(ValidationError, match="permission files"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
            whitelist=value,
        )


def test_permission_files_preserve_blank_lines_and_unset_metadata(
    tmp_path: Path,
) -> None:
    cluster = make_cluster()
    cluster = ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        adminlist="\nKU_admin\n\n",
        downloads=cluster.downloads,
    )
    cluster.save(tmp_path)
    admin = tmp_path / "adminlist.txt"
    assert admin.read_bytes() == b"\nKU_admin\n\n"

    admin.chmod(0o640)
    os.utime(admin, ns=(1_000_000_000, 1_000_000_000))
    before = admin.stat()
    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        downloads=cluster.downloads,
    ).save(tmp_path)

    after = admin.stat()
    assert admin.read_bytes() == b"\nKU_admin\n\n"
    assert after.st_mode & 0o777 == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns


def test_mod_prepare_preserves_generated_downloads(tmp_path: Path) -> None:
    cluster = tmp_path / "cluster"
    make_cluster().save(cluster)

    assert mods.prepare_shared(cluster) == (8, 42)
    assert (cluster / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == ('ServerModSetup("8")\nServerModSetup("42")\nServerModCollectionSetup("99")\n')


def test_mod_prepare_preserves_native_setup_byte_for_byte(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "cluster"
    shard = cluster / "Master"
    mods_path = cluster / "mods"
    shard.mkdir(parents=True)
    (shard / "server.ini").touch()
    mods_path.mkdir()
    (shard / "modoverrides.lua").write_text(
        'return { ["workshop-42"] = { enabled = true } }',
        encoding="utf-8",
    )
    setup = mods_path / "dedicated_server_mods_setup.lua"
    original = (
        "#!/usr/bin/env luajit\r\n"
        'local collection = "99"\r\n'
        "ServerModCollectionSetup(collection)\r\n"
        '--[[\r\nServerModSetup("42")\r\n]]\r\n'
        'ServerModSetup("8"); return ServerModSetup("9")\r\n'
    )
    setup.write_text(original, encoding="utf-8")
    assert mods.prepare_shared(cluster) == (8, 9)
    assert setup.read_bytes() == original.encode()


def test_mod_override_parser_accepts_empty_files_and_rejects_truthy_numbers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "modoverrides.lua"
    path.write_text("-- no overrides\n", encoding="utf-8")
    assert ModOverrides.load(path).workshop_items == frozenset()

    path.write_text(
        'return { ["workshop-42"] = { enabled = 1 } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="valid boolean"):
        ModOverrides.load(path)


def test_mod_file_parsers_enforce_static_workshop_boundaries(tmp_path: Path) -> None:
    override = tmp_path / "modoverrides.lua"
    override.write_text(
        'return { ["workshop-18446744073709551616"] = { enabled = true } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid DST Workshop mod name"):
        ModOverrides.load(override)

    override.write_text(
        'return { [("workshop-" .. "42")] = { enabled = true } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="string"):
        ModOverrides.load(override)

    invalid_overrides = (
        ("return { client_mods_disabled = 1 }", "valid boolean"),
        ('return { ["local"] = true }', "valid dictionary"),
        (
            'return { ["workshop-42"] = { enabled = true, enabled = false } }',
            "duplicate key",
        ),
        (
            'return { ["workshop-42"] = {}, ["workshop-42"] = {} }',
            "duplicate key",
        ),
    )
    for content, message in invalid_overrides:
        override.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            ModOverrides.load(override)

    setup = tmp_path / "dedicated_server_mods_setup.lua"
    setup.write_text(
        'ServerModSetup("18446744073709551616")\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="uint64"):
        mods.scan_setup(setup)


def test_cluster_rejects_token_line_injection() -> None:
    with pytest.raises(ValidationError, match="cluster tokens"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
            token=SecretStr("first\nsecond"),
        )


@pytest.mark.parametrize("token", ["contains space", "tab\there", "令牌"])
def test_cluster_rejects_tokens_the_game_would_silently_filter(token: str) -> None:
    with pytest.raises(ValidationError, match="printable non-space ASCII"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
            token=SecretStr(token),
        )


def test_cluster_save_refuses_unmanaged_shard_directory(tmp_path: Path) -> None:
    (tmp_path / "OldCaves").mkdir()

    with pytest.raises(ValueError, match="would remain active"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(tmp_path)


def test_cluster_save_preflights_managed_paths_before_replacing_files(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "cluster"
    mods_path = cluster / "mods"
    mods_path.mkdir(parents=True)
    original = "[NETWORK]\ncluster_name = original\n"
    (cluster / "cluster.ini").write_text(original, encoding="utf-8")
    (mods_path / "ugc").write_text("conflict", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(cluster)

    assert (cluster / "cluster.ini").read_text(encoding="utf-8") == original


def test_cluster_save_rejects_shard_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "cluster"
    target = tmp_path / "outside"
    cluster.mkdir()
    target.mkdir()
    (cluster / "cluster.ini").write_text("original", encoding="utf-8")
    (cluster / "Master").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(cluster)

    assert list(target.iterdir()) == []
    assert (cluster / "cluster.ini").read_text(encoding="utf-8") == "original"


def test_cluster_save_rejects_symlink_root_before_reading_files(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    mods_path = outside / "mods"
    mods_path.mkdir(parents=True)
    (mods_path / "modsettings.lua").write_text("", encoding="utf-8")
    (mods_path / "dedicated_server_mods_setup.lua").write_text(
        "invalid lua !",
        encoding="utf-8",
    )
    root = tmp_path / "cluster"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="root cannot be a symlink"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(root)


@pytest.mark.parametrize(
    ("root_key", "override_key"),
    [(None, None), ("existing", None), (None, "override"), ("unused", "override")],
)
def test_cluster_save_materializes_and_preserves_shared_key(
    tmp_path: Path, root_key: str | None, override_key: str | None
) -> None:
    template = FOREST_CAVES.build(token=SecretStr(""))
    original = template.replace(
        settings=template.settings.replace(
            cluster_key=SecretStr(root_key) if root_key is not None else None
        ),
        shards={
            name: shard.replace(
                settings=shard.settings.replace(
                    cluster_key=(
                        SecretStr(override_key) if override_key is not None else None
                    )
                )
            )
            for name, shard in template.shards.items()
        },
    )
    original.save(tmp_path / "first")
    persisted = ClusterConfig.load(tmp_path / "first")
    key = override_key or root_key
    if key is None:
        assert persisted.settings.cluster_key is not None
        key = persisted.settings.cluster_key.get_secret_value()
        assert len(key) >= 32
    template.save(tmp_path / "first")
    loaded = ClusterConfig.load(tmp_path / "first")
    assert loaded.settings.cluster_key == SecretStr(key)
    assert all(shard.settings.cluster_key is None for shard in loaded.shards.values())
    template.replace(settings=template.settings.replace(pvp=True)).save(
        tmp_path / "first"
    )
    assert ClusterConfig.load(tmp_path / "first").settings.cluster_key == SecretStr(key)
    template.save(tmp_path / "second")
    assert ClusterConfig.load(tmp_path / "second").settings.cluster_key not in {
        None,
        SecretStr(key),
    }
    assert template.settings.cluster_key is None
    assert "cluster_key" not in template.files()[Path("cluster.ini")]


@pytest.mark.parametrize("unsafe_path", [".", "cluster.ini", "forest/server.ini"])
def test_cluster_key_lookup_rejects_symlinks(
    tmp_path: Path, unsafe_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cluster"
    target = tmp_path / "outside"
    if unsafe_path == ".":
        target.mkdir()
    else:
        target.write_text("unreadable configuration", encoding="utf-8")
    link = root / unsafe_path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)

    def unexpected_load(path: Path) -> ClusterSettings:
        pytest.fail(f"read unsafe configuration: {path}")

    monkeypatch.setattr(ClusterSettings, "load", unexpected_load)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        FOREST_CAVES.build(token=SecretStr("")).save(root)


def test_saving_existing_configuration_does_not_require_mod_download_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired = FOREST_CAVES.build(token=SecretStr("test-token"))
    desired.save(tmp_path)
    monkeypatch.setitem(sys.modules, "dst_server.mods", None)

    desired.save(tmp_path)

    assert ClusterConfig.load(tmp_path).shards.keys() == desired.shards.keys()
