from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.overrides import (
    WorldgenOverride,
)
from dst_server.configuration.presets import (
    compose,
    shard,
)


def test_ini_defaults_are_omitted_except_encoded_user_paths(
    tmp_path: Path,
) -> None:
    cluster_settings = ClusterSettings()
    assert cluster_settings.render() == ""
    assert cluster_settings.render(multi_shard=True) == (
        "[SHARD]\nshard_enabled = true\n"
    )
    assert cluster_settings.shard_enabled is False
    assert cluster_settings.model_dump(exclude_unset=True) == {}
    server_ini = tmp_path / "server.ini"
    server_ini.write_text("", encoding="utf-8")
    loaded_shard = ShardSettings.load(server_ini)
    assert loaded_shard.is_master is True
    assert loaded_shard.model_fields_set == set()
    account = "[ACCOUNT]\nencode_user_path = true\n"
    assert loaded_shard.render() == account
    assert loaded_shard.model_dump(exclude_unset=True) == {}
    assert ShardSettings().render(multi_shard=True) == (
        "[SHARD]\nis_master = true\n\n" + account
    )
    assert ShardSettings(is_master=True).encode_user_path is True
    assert ShardSettings(is_master=True).render() == (
        "[SHARD]\nis_master = true\n\n" + account
    )
    raw_user_paths = "[ACCOUNT]\nencode_user_path = false\n"
    assert ShardSettings(encode_user_path=False).render() == raw_user_paths
    server_ini.write_text(raw_user_paths, encoding="utf-8")
    loaded_shard = ShardSettings.load(server_ini)
    assert loaded_shard.encode_user_path is False
    assert loaded_shard.render() == raw_user_paths
    settings = ClusterSettings(
        max_snapshots=6,
        offline_cluster=False,
        max_players=16,
        pause_when_empty=False,
        cluster_password=SecretStr(""),
    )
    expected = (
        "[MISC]\n"
        "max_snapshots = 6\n"
        "\n"
        "[NETWORK]\n"
        "cluster_password = \n"
        "offline_cluster = false\n"
        "\n"
        "[GAMEPLAY]\n"
        "max_players = 16\n"
        "pause_when_empty = false\n"
    )

    assert settings.render() == expected
    path = tmp_path / "cluster.ini"
    path.write_text(expected.lower(), encoding="utf-8")
    loaded = ClusterSettings.load(path)

    assert loaded == settings
    assert loaded.model_fields_set == settings.model_fields_set
    assert loaded.render() == expected


def test_required_ini_fields_do_not_override_preset_values() -> None:
    patch = ShardSettings(server_port=12000)
    patch.render(multi_shard=True)

    settings = (
        compose(
            shard("forest", settings=ShardSettings(encode_user_path=False)),
            shard("forest", settings=patch),
        )
        .shards["forest"]
        .settings
    )

    assert patch.model_fields_set == {"server_port"}
    assert settings.server_port == 12000
    assert settings.encode_user_path is False


def test_ini_loader_rejects_unknown_input(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text("[NETWORK]\nfuture_option = true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown DST INI option"):
        ClusterSettings.load(path)


@pytest.mark.parametrize(
    ("source", "line", "error"),
    [
        ("cluster_password = PRIVATE_INI_SECRET\n", 1, "MissingSectionHeaderError"),
        ("[NETWORK]\ncluster_password PRIVATE_INI_SECRET\n", 2, "ParsingError"),
        (
            (
                "[NETWORK]\ncluster_password = PRIVATE_INI_SECRET\n"
                "cluster_password = PRIVATE_INI_SECRET\n"
            ),
            3,
            "DuplicateOptionError",
        ),
        (
            "[NETWORK]\ncluster_password = PRIVATE_INI_SECRET\n[NETWORK]\n",
            3,
            "DuplicateSectionError",
        ),
    ],
)
def test_ini_syntax_errors_hide_contents_and_preserve_file_location(
    tmp_path: Path, source: str, line: int, error: str
) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text(source)
    with pytest.raises(ValueError, match="invalid DST INI configuration") as caught:
        ClusterSettings.load(path)
    diagnostic = str(caught.value)
    assert "PRIVATE_INI_SECRET" not in diagnostic
    assert str(path) in diagnostic
    assert f"{error} at line {line}" in diagnostic


@pytest.mark.parametrize("game_mode", ["endless", "wilderness"])
def test_ini_loader_warns_and_preserves_deprecated_game_modes(
    tmp_path: Path,
    game_mode: str,
) -> None:
    path = tmp_path / "cluster.ini"
    source = f"[GAMEPLAY]\ngame_mode = {game_mode}\n"
    path.write_text(source, encoding="utf-8")

    with pytest.warns(FutureWarning, match=f"game_mode='{game_mode}' is deprecated"):
        settings = ClusterSettings.load(path)

    assert settings.game_mode == game_mode
    assert settings.render() == source


def test_ini_loader_preserves_mod_game_mode(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    source = "[GAMEPLAY]\ngame_mode = modded_mode\n"
    path.write_text(source, encoding="utf-8")

    settings = ClusterSettings.load(path)

    assert settings.game_mode == "modded_mode"
    assert settings.render() == source


def test_shard_loader_discards_legacy_authentication_port(tmp_path: Path) -> None:
    path = tmp_path / "server.ini"
    path.write_text("[STEAM]\nauthentication_port = 8766\n", encoding="utf-8")

    settings = ShardSettings.load(path)

    assert settings.model_fields_set == set()
    assert settings.render() == "[ACCOUNT]\nencode_user_path = true\n"
    assert "authentication_port" not in settings.model_dump()


def test_ini_models_reject_non_utf8_text() -> None:
    with pytest.raises(ValidationError):
        ClusterSettings(cluster_name="\ud800")
    with pytest.raises(ValidationError):
        ShardSettings(name="\ud800")


def test_ini_loader_matches_native_boolean_and_network_parsing(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text(
        "[NETWORK]\n"
        "connection_timeout = 42\n"
        "internet_broadcasting_enabled = FALSE\n"
        "idle_timeout = 7\n"
        "[GAMEPLAY]\n"
        "pvp = TrUe\n",
        encoding="utf-8",
    )

    settings = ClusterSettings.load(path)

    assert settings.connection_timeout == 42
    assert settings.internet_broadcasting_enabled is False
    assert settings.idle_timeout == 7
    assert settings.pvp is True
    path.write_text("[GAMEPLAY]\npvp = yes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="true or false"):
        ClusterSettings.load(path)


def test_tick_rate_one_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text("[NETWORK]\ntick_rate = 1\n", encoding="utf-8")

    settings = ClusterSettings.load(path)

    assert settings.tick_rate == 1
    assert settings.render() == "[NETWORK]\ntick_rate = 1\n"
    for tick_rate in (0, 61):
        with pytest.raises(ValidationError):
            ClusterSettings(tick_rate=tick_rate)


def test_native_misc_booleans_load_and_render_when_explicit(tmp_path: Path) -> None:
    assert ClusterSettings(use_alternate_gc=False, mods_enabled=True).render() == (
        "[MISC]\nuse_alternate_gc = false\nmods_enabled = true\n"
    )
    path = tmp_path / "cluster.ini"
    path.write_text(
        "[MISC]\nuse_alternate_gc = TRUE\nmods_enabled = false\n",
        encoding="utf-8",
    )

    settings = ClusterSettings.load(path)

    assert settings.use_alternate_gc is True
    assert settings.mods_enabled is False
    assert settings.render() == (
        "[MISC]\nuse_alternate_gc = true\nmods_enabled = false\n"
    )


def test_network_language_and_unsupported_dns_are_typed() -> None:
    settings = ClusterSettings(cluster_language="zht", override_dns="1.1.1.1")

    assert settings.render() == (
        "[NETWORK]\ncluster_language = zht\noverride_dns = 1.1.1.1\n"
    )
    with pytest.raises(ValidationError):
        ClusterSettings(cluster_language="ja")  # ty: ignore[invalid-argument-type]


def test_native_ini_numeric_boundaries() -> None:
    defaults = ClusterSettings()

    assert defaults.connection_timeout == 8000
    assert defaults.internet_broadcasting_enabled is True
    assert defaults.idle_timeout == 1800
    assert (
        ShardSettings(
            is_master=False,
            name="Secondary",
            id=2**32 - 1,
        ).id
        == 2**32 - 1
    )
    with pytest.raises(ValidationError):
        ClusterSettings(max_snapshots=0)
    with pytest.raises(ValidationError):
        ClusterSettings.model_validate({"idle_timeout": 0.5})
    with pytest.raises(ValidationError):
        ShardSettings(is_master=False, name="Secondary", id=2**32)


def test_shared_configuration_types_preserve_constraints() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        ClusterSettings(cluster_key=SecretStr("bad\nsecret"))
    with pytest.raises(ValidationError, match="254"):
        ClusterSettings(cluster_password=SecretStr("x" * 255))
    cluster_key = ShardSettings(
        is_master=True,
        cluster_key=SecretStr("x" * 255),
    ).cluster_key
    assert cluster_key is not None
    assert cluster_key.get_secret_value() == "x" * 255
    with pytest.raises(ValidationError):
        WorldgenOverride(worldgen_preset="bad\npreset")


@pytest.mark.parametrize(
    ("shards", "message"),
    [
        (
            {
                "one": ShardConfig(settings=ShardSettings(is_master=False)),
            },
            "exactly one master",
        ),
        (
            {
                "one": ShardConfig(settings=ShardSettings(is_master=True)),
                "two": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "exactly one master",
        ),
        (
            {
                "mods": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
        (
            {
                "cluster.ini": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
        (
            {
                "\ud800": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
        (
            {
                "x" * 256: ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
    ],
)
def test_cluster_rejects_invalid_topology(
    shards: dict[str, ShardConfig],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ClusterConfig(shards=shards)


def test_multi_shard_requires_explicit_non_conflicting_network() -> None:
    master = ShardConfig(settings=ShardSettings(is_master=True))
    caves = ShardConfig(settings=ShardSettings(is_master=False, name="Caves"))

    with pytest.raises(ValidationError, match="master_ip"):
        ClusterConfig(shards={"Master": master, "Caves": caves})

    assert (
        ClusterConfig(
            settings=ClusterSettings(shard_enabled=True),
            shards={"Master": master},
        ).settings.cluster_key
        is None
    )

    with pytest.raises(ValidationError, match="UDP port"):
        ClusterConfig(
            settings=ClusterSettings(
                master_ip="127.0.0.1",
                cluster_key=SecretStr("key"),
            ),
            shards={"Master": master, "Caves": caves},
        )

    with pytest.raises(ValidationError, match="cluster_key"):
        ClusterConfig(
            settings=ClusterSettings(
                master_ip="127.0.0.1",
                cluster_key=SecretStr("key"),
            ),
            shards={
                "Master": master,
                "Caves": caves.replace(
                    settings=caves.settings.replace(cluster_key=SecretStr(""))
                ),
            },
        )

    with pytest.raises(ValidationError, match="cluster_key"):
        ClusterConfig(
            settings=ClusterSettings(master_ip="127.0.0.1"),
            shards={
                "Master": master,
                "Caves": caves.replace(
                    settings=caves.settings.replace(cluster_key=SecretStr("key"))
                ),
            },
        )


def test_shard_ids_follow_master_and_secondary_roles() -> None:
    with pytest.raises(ValidationError, match="master shard id must be 1"):
        ShardSettings(is_master=True, id=2)
    with pytest.raises(ValidationError, match="secondary shard ids must be at least 2"):
        ShardSettings(is_master=False, name="Caves", id=1)

    ClusterConfig(
        settings=ClusterSettings(
            master_ip="127.0.0.1",
            cluster_key=SecretStr("key"),
        ),
        shards={
            "Master": ShardConfig(settings=ShardSettings(is_master=True, id=1)),
            "Caves": ShardConfig(
                settings=ShardSettings(
                    is_master=False,
                    name="Caves",
                    id=2,
                    master_server_port=27017,
                    server_port=11000,
                )
            ),
        },
    )
