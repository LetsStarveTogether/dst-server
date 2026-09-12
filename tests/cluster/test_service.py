from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server import cli as cluster_cli
from dst_server import mods
from dst_server.cluster import daemon, service
from dst_server.mods import native
from dst_server.runtime import ServerConfig
from dst_server.telemetry import TelemetrySettings, otel


@pytest.mark.parametrize("profile", ["off", "critical", "history"])
def test_cli_reads_telemetry_profile_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    serving = AsyncMock(return_value=7)
    monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", profile)
    monkeypatch.setattr(daemon, "serve", serving)

    assert cluster_cli.main(("agent", "serve", "forest")) == 7
    serving.assert_awaited_once_with(
        telemetry=TelemetrySettings.model_validate({"profile": profile}),
        shard="forest",
        external_port=None,
    )


@pytest.mark.parametrize(
    ("command", "target", "expected"),
    [
        (("prepare",), "prepare_shared", {"update_mods": True}),
        (
            ("serve", "--external-port", "30007", "cave"),
            "serve",
            {
                "telemetry": TelemetrySettings(),
                "shard": "cave",
                "external_port": 30007,
            },
        ),
        (
            ("master", "--external-port", "30000"),
            "master",
            {"telemetry": TelemetrySettings(), "external_port": 30000},
        ),
    ],
)
def test_cli_routes_commands_once(
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    target: str,
    expected: dict[str, object],
) -> None:
    action = AsyncMock(return_value=0)
    monkeypatch.setattr(
        service if target == "prepare_shared" else daemon, target, action
    )

    assert cluster_cli.main(("agent", *command)) == 0
    action.assert_awaited_once_with(**expected)


@pytest.mark.parametrize(
    ("command", "profile", "exit_code"),
    [
        (("serve", "--external-port", "1023", "cave"), None, 2),
        (("serve", "--master", "forest"), None, 2),
        (("serve", "forest"), "verbose", 1),
    ],
)
def test_cli_rejects_invalid_arguments_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    profile: str | None,
    exit_code: int,
) -> None:
    if profile is not None:
        monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", profile)

    assert cluster_cli.main(("agent", *command)) == exit_code


def test_otel_resource_uses_explicit_cluster_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = object()
    configure = Mock(return_value=pipeline)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
    monkeypatch.setattr(otel, "configure", configure)

    config = ServerConfig(shard="forest", telemetry_cluster="dst-042")
    assert service.configure_otel(config) is pipeline
    configure.assert_called_once_with(
        resource_attributes={"dst.cluster.name": "dst-042"},
    )


def mod_service_paths(tmp_path: Path, setup: str) -> tuple[Path, Path]:
    install = tmp_path / "install"
    executable = install / service.EXECUTABLE
    executable.parent.mkdir(parents=True)
    executable.touch()
    cluster = tmp_path / "cluster"
    shard = cluster / "forest"
    shard.mkdir(parents=True)
    (shard / "server.ini").write_text("[SHARD]\nis_master = true\nid = 1\n")
    (shard / "modoverrides.lua").write_text(
        'return { ["workshop-7"] = { enabled = true } }'
    )
    (cluster / "cluster.ini").touch()
    (cluster / "cluster_token.txt").touch()
    (cluster / "mods").mkdir()
    (cluster / "mods" / "dedicated_server_mods_setup.lua").write_text(setup)
    return install, cluster


async def test_prepare_keeps_native_as_default_and_accepts_dynamic_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, cluster = mod_service_paths(
        tmp_path, 'local id = "42"; ServerModSetup(id)'
    )
    update = AsyncMock()
    monkeypatch.setenv("DST_SERVER_MOD_PROXY", "http://download.invalid:1080")
    monkeypatch.setattr(native, "update", update)

    override = cluster / "forest/modoverrides.lua"
    override.write_text(
        'local id = "42"; return {["workshop-" .. id] = {enabled = true}}'
    )
    setup = cluster / "mods/dedicated_server_mods_setup.lua"
    original = {path: path.read_bytes() for path in (override, setup)}

    shards = await service.prepare_shared(install, cluster)

    assert {path: path.read_bytes() for path in original} == original
    update.assert_awaited_once()
    assert update.call_args.kwargs["proxy"] == "http://download.invalid:1080"
    assert update.call_args.args == (
        install / service.EXECUTABLE,
        cluster / "mods" / "ugc",
    )
    assert (install / "mods").resolve() == cluster / "mods"
    assert service.create_server_config(install, cluster, shards[0]).extra_args == (
        "-skip_update_server_mods",
    )


@pytest.mark.parametrize("update", [False, True])
async def test_preparation_skips_download_when_not_requested_or_no_mods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: bool
) -> None:
    install, cluster = mod_service_paths(tmp_path, "")
    (cluster / "forest/modoverrides.lua").write_text("return {}")
    download = AsyncMock()
    monkeypatch.setattr(native, "update", download)
    await mods.prepare(install, cluster, update=update)
    download.assert_not_awaited()


async def test_startup_preserves_native_configuration_and_saved_world(
    tmp_path: Path,
) -> None:
    from dst_server.configuration import files
    from dst_server.configuration.store import ConfigurationStore

    install, cluster = mod_service_paths(tmp_path, "")
    world = cluster / "forest/worldgenoverride.lua"
    world.write_text('local preset = "SURVIVAL_TOGETHER"; return {preset = preset}')
    saved = cluster / "forest/save/world"
    saved.parent.mkdir()
    saved.write_bytes(b"existing world")
    inactive = cluster / "cave"
    inactive.mkdir()
    (inactive / "modoverrides.lua").write_text("inactive Lua")
    before = {
        path: path.read_bytes()
        for path in (world, saved, inactive / "modoverrides.lua")
    }
    expected = (files.Shard("forest", True),)

    assert ConfigurationStore(cluster).shards == expected
    assert files.discover(cluster) == expected
    assert await service.prepare_shared(install, cluster, update_mods=False) == expected

    assert {path: path.read_bytes() for path in before} == before
