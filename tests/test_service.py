from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.cluster import cli as cluster_cli
from dst_server.cluster import daemon, mods, service
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

    assert cluster_cli.main(("serve", "forest")) == 7
    serving.assert_awaited_once_with(
        telemetry=TelemetrySettings.model_validate({"profile": profile}),
        shard="forest",
        external_port=None,
    )


@pytest.mark.parametrize(
    ("command", "target", "expected"),
    [
        (("prepare",), "prepare_shared", {}),
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

    assert cluster_cli.main(command) == 0
    action.assert_awaited_once_with(**expected)


@pytest.mark.parametrize(
    ("command", "profile", "error"),
    [
        (("serve", "--external-port", "1023", "cave"), None, SystemExit),
        (("serve", "--master", "forest"), None, SystemExit),
        (("serve", "forest"), "verbose", ValueError),
    ],
)
def test_cli_rejects_invalid_arguments_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    profile: str | None,
    error: type[BaseException],
) -> None:
    if profile is not None:
        monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", profile)

    with pytest.raises(error):
        cluster_cli.main(command)


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
        outbox_path=Path("/cluster/forest/.telemetry.sqlite3"),
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


@pytest.mark.parametrize("backend", [None, "native"])
async def test_prepare_keeps_native_as_default_and_accepts_dynamic_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str | None,
) -> None:
    install, cluster = mod_service_paths(
        tmp_path, 'local id = "42"; ServerModSetup(id)'
    )
    if backend is None:
        monkeypatch.delenv("DST_SERVER_MOD_UPDATER", raising=False)
    else:
        monkeypatch.setenv("DST_SERVER_MOD_UPDATER", backend)
    update = AsyncMock()
    monkeypatch.setattr(mods, "update", update)

    shards = await service.prepare_shared(install, cluster)

    update.assert_awaited_once()
    assert update.call_args.args == (
        install / service.EXECUTABLE,
        cluster / "mods" / "ugc",
    )
    assert (install / "mods").resolve() == cluster / "mods"
    assert service.create_server_config(install, cluster, shards[0]).extra_args == (
        "-skip_update_server_mods",
    )


@pytest.mark.parametrize(
    ("explicit", "directory", "expected"),
    [
        ("/chosen/steamcmd", "/other", "/chosen/steamcmd"),
        (None, "/chosen", "/chosen/steamcmd.sh"),
        (None, None, "/path/steamcmd"),
    ],
)
async def test_prepare_selects_steamcmd_and_passes_items_and_collections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    directory: str | None,
    expected: str,
) -> None:
    from dst_server import workshop

    install, cluster = mod_service_paths(
        tmp_path, 'ServerModSetup("42"); return ServerModCollectionSetup("99")'
    )
    monkeypatch.setenv("DST_SERVER_MOD_UPDATER", "steamcmd")
    for name, value in (
        ("DST_SERVER_STEAMCMD", explicit),
        ("STEAMCMDDIR", directory),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    which = Mock(
        side_effect=lambda executable: (
            "/path/steamcmd" if executable == "steamcmd" else executable
        )
    )
    monkeypatch.setattr(service.shutil, "which", which)
    updaters = [Mock(update=AsyncMock()), Mock(update=AsyncMock())]
    factory = Mock(side_effect=updaters)
    monkeypatch.setattr(workshop, "WorkshopUpdater", factory)
    native = AsyncMock()
    monkeypatch.setattr(mods, "update", native)

    await service.prepare_shared(install, cluster)
    await service.prepare_shared(install, cluster)

    assert factory.call_count == 2
    for updater, call in zip(updaters, factory.call_args_list, strict=True):
        updater.update.assert_awaited_once_with((7, 42), collections=(99,))
        client, destination = call.args
        assert client.executable == expected
        assert callable(client.log_handler)
        assert destination == cluster / "mods"
    native.assert_not_awaited()
    assert (install / "mods").resolve() == cluster / "mods"
    requested = (
        expected if explicit is not None or directory is not None else "steamcmd"
    )
    assert which.call_args_list == [((requested,), {}), ((requested,), {})]


async def test_prepare_rejects_unknown_mod_updater_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, cluster = mod_service_paths(tmp_path, 'ServerModSetup("42")')
    monkeypatch.setenv("DST_SERVER_MOD_UPDATER", "unknown")

    with pytest.raises(ValueError, match="DST_SERVER_MOD_UPDATER"):
        await service.prepare_shared(install, cluster)

    assert not (install / "mods").exists()
    assert not (cluster / "mods" / "ugc").exists()


@pytest.mark.parametrize(
    ("setup", "error", "message"),
    [
        ('local id = "42"; ServerModSetup(id)', ValueError, "requires only static"),
        ('ServerModSetup("42")', FileNotFoundError, "SteamCMD executable not found"),
    ],
)
async def test_prepare_rejects_unsupported_steamcmd_configuration_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    error: type[Exception],
    message: str,
) -> None:
    install, cluster = mod_service_paths(tmp_path, setup)
    monkeypatch.setenv("DST_SERVER_MOD_UPDATER", "steamcmd")
    monkeypatch.delenv("DST_SERVER_STEAMCMD", raising=False)
    monkeypatch.delenv("STEAMCMDDIR", raising=False)
    monkeypatch.setattr(service.shutil, "which", lambda _name: None)

    with pytest.raises(error, match=message):
        await service.prepare_shared(install, cluster)

    assert not (install / "mods").exists()


async def test_prepare_without_updates_needs_no_steamcmd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, cluster = mod_service_paths(
        tmp_path, 'local id = "42"; ServerModSetup(id)'
    )
    monkeypatch.setenv("DST_SERVER_MOD_UPDATER", "steamcmd")
    which = Mock(side_effect=AssertionError("unexpected executable lookup"))
    monkeypatch.setattr(service.shutil, "which", which)

    await service.prepare_shared(install, cluster, update_mods=False)

    which.assert_not_called()
    assert not (install / "mods").exists()


async def test_empty_setup_template_needs_no_steamcmd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, cluster = mod_service_paths(tmp_path, 'ServerModSetup("")')
    (cluster / "forest" / "modoverrides.lua").write_text("return {}")
    monkeypatch.setenv("DST_SERVER_MOD_UPDATER", "steamcmd")
    which = Mock(side_effect=AssertionError("unexpected executable lookup"))
    monkeypatch.setattr(service.shutil, "which", which)

    await service.prepare_shared(install, cluster)

    which.assert_not_called()


async def test_failed_steamcmd_update_preserves_previous_install_mods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dst_server import workshop

    install, cluster = mod_service_paths(tmp_path, 'ServerModSetup("42")')
    previous_mods = install / "mods"
    previous_mods.mkdir()
    sentinel = previous_mods / "keep"
    sentinel.write_bytes(b"previous installation")
    monkeypatch.setenv("DST_SERVER_MOD_UPDATER", "steamcmd")
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/path/steamcmd")
    update = AsyncMock(side_effect=RuntimeError("download failed"))
    monkeypatch.setattr(
        workshop, "WorkshopUpdater", Mock(return_value=Mock(update=update))
    )

    with pytest.raises(RuntimeError, match="download failed"):
        await service.prepare_shared(install, cluster)

    assert not previous_mods.is_symlink()
    assert sentinel.read_bytes() == b"previous installation"
