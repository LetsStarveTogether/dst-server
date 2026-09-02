import os
import stat
from pathlib import Path

import pytest

import dst_server.cluster as cluster_api
from dst_server.cluster import console, mods, service


def write_shard(path: Path, *, is_master: bool, name: str) -> None:
    path.mkdir()
    (path / "server.ini").write_text(
        f"[SHARD]\nis_master = {str(is_master).lower()}\nname = {name}\nid = 1\n",
        encoding="utf-8",
    )


@pytest.fixture
def service_layout(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "install"
    executable = install / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)
    cluster = tmp_path / "000"
    cluster.mkdir()
    for name in ("cluster.ini", "cluster_token.txt"):
        (cluster / name).touch()
    return install, cluster


def test_default_deployment_is_a_typed_pod_application() -> None:
    deploy = Path(__file__).parents[1] / "deploy"
    quadlet = deploy / "quadlet"
    application = cluster_api.QuadletApplication.load(
        quadlet,
        name="dst-000",
    )
    rendered = "\n".join(application.files().values())

    assert len(application.secondaries) == 1
    assert application.pod.networks == ()
    assert not tuple(quadlet.glob("*.network"))
    master = application.master
    secondary = application.secondaries[0]
    assert master.name == "dst-000-forest"
    assert master.exec[1] == "master"
    assert secondary.exec[1:] == ("serve", "--external-port", "30002", "--", "cave")
    assert master.wants == (f"{secondary.name}.container",)
    assert secondary.after == secondary.binds_to == (f"{master.name}.container",)
    assert tuple(
        (mapping.host, mapping.container) for mapping in application.pod.publish_ports
    ) == (
        (30000, 10999),
        (30001, 27016),
        (30002, 11000),
        (30003, 27017),
    )
    assert master.exec[3] == "30000"
    assert "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://10.255.255.254:4317" in rendered
    assert "DST_SERVER_TELEMETRY_PROFILE=history" in rendered
    assert "OTEL_METRICS_EXPORTER=none" in rendered
    assert "OTEL_TRACES_EXPORTER=none" in rendered


def test_netdata_deployment_contract_is_consistent() -> None:
    deploy = Path(__file__).parents[1] / "deploy"
    application = cluster_api.QuadletApplication.load(
        deploy / "quadlet", name="dst-000"
    )
    address = dict(application.master.environment)[
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
    ].removeprefix("http://")
    base_dir = "/data/netdata/log/otel"
    otel = (deploy / "netdata/otel.yaml").read_text(encoding="utf-8")

    assert f'path: "{address}"' in otel
    assert f'base_dir: "{base_dir}"' in otel
    assert f"Address={address.rsplit(':', 1)[0]}/32" in (
        deploy / "networkd/10-netdata-loopback.network"
    ).read_text(encoding="utf-8")
    assert f"RequiresMountsFor={base_dir}" in (
        deploy / "netdata/netdata.service.d/dependencies.conf"
    ).read_text(encoding="utf-8")
    assert "bind to = localhost" in (deploy / "netdata/netdata.conf").read_text(
        encoding="utf-8"
    )


def test_cluster_and_mod_files_are_prepared(tmp_path: Path) -> None:
    install = tmp_path / "install"
    cluster = tmp_path / "cluster"
    install_mods = install / "mods"
    install_mods.mkdir(parents=True)
    (install_mods / "old").touch()
    cluster.mkdir()
    (cluster / "cluster.ini").touch()
    (cluster / "cluster_token.txt").touch()
    write_shard(cluster / "forest", is_master=True, name="Forest")
    write_shard(cluster / "cave", is_master=False, name="Caves")
    (cluster / "forest" / "modoverrides.lua").write_text(
        'return { ["workshop-42"] = { enabled = true }, '
        '["workshop-7"] = { enabled = true } }',
        encoding="utf-8",
    )
    (cluster / "cave" / "modoverrides.lua").write_text(
        'return { ["workshop-42"] = { enabled = true } }',
        encoding="utf-8",
    )

    cluster_api.prepare(cluster)
    mod_ids = mods.prepare_shared(cluster)
    mods.activate(install, cluster)
    shards = cluster_api.discover(cluster)

    assert mod_ids == (7, 42)
    assert install_mods.is_symlink()
    assert install_mods.resolve() == (cluster / "mods").resolve()
    assert (cluster / "mods" / "ugc").is_dir()
    assert (cluster / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == 'ServerModSetup("7")\nServerModSetup("42")\n'
    assert {shard.name for shard in shards} == {"forest", "cave"}
    assert (
        next(shard for shard in shards if shard.master).console == cluster / "console"
    )
    assert all(
        (cluster / name).is_file()
        for name in ("adminlist.txt", "blocklist.txt", "whitelist.txt")
    )


def test_prepare_preserves_existing_permission_files(tmp_path: Path) -> None:
    for name in ("cluster.ini", "cluster_token.txt"):
        (tmp_path / name).touch()
    paths = tuple(
        tmp_path / name for name in ("adminlist.txt", "blocklist.txt", "whitelist.txt")
    )
    for path in paths:
        path.touch()
        os.utime(path, ns=(0, 0))

    cluster_api.prepare(tmp_path)

    assert all(path.stat().st_mtime_ns == 0 for path in paths)


def test_prepare_rejects_permission_symlinks(tmp_path: Path) -> None:
    for name in ("cluster.ini", "cluster_token.txt"):
        (tmp_path / name).touch()
    outside = tmp_path / "outside"
    (tmp_path / "whitelist.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="permission file cannot be a symlink"):
        cluster_api.prepare(tmp_path)

    assert not outside.exists()
    assert not (tmp_path / "adminlist.txt").exists()
    assert not (tmp_path / "blocklist.txt").exists()


@pytest.mark.parametrize("required", ["cluster.ini", "cluster_token.txt"])
def test_prepare_requires_cluster_configuration(
    tmp_path: Path,
    required: str,
) -> None:
    other = {"cluster.ini", "cluster_token.txt"} - {required}
    (tmp_path / other.pop()).touch()

    with pytest.raises(FileNotFoundError, match=required):
        cluster_api.prepare(tmp_path)


@pytest.mark.parametrize(
    ("master_flags", "message"),
    [
        pytest.param((), "no DST shard directories", id="empty"),
        pytest.param((False, False), "exactly one master", id="no-master"),
        pytest.param((True, True), "exactly one master", id="two-masters"),
    ],
)
def test_discovery_requires_one_master(
    tmp_path: Path,
    master_flags: tuple[bool, ...],
    message: str,
) -> None:
    for index, is_master in enumerate(master_flags):
        write_shard(
            tmp_path / str(index),
            is_master=is_master,
            name=str(index),
        )

    with pytest.raises(ValueError, match=message):
        cluster_api.discover(tmp_path)


def test_discovery_rejects_native_invalid_boolean_and_shard_symlink(
    tmp_path: Path,
) -> None:
    shard = tmp_path / "forest"
    write_shard(shard, is_master=True, name="Forest")
    (shard / "server.ini").write_text(
        "[SHARD]\nis_master = yes\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="true or false"):
        cluster_api.discover(tmp_path)

    for path in shard.iterdir():
        path.unlink()
    shard.rmdir()
    outside = tmp_path / "outside"
    write_shard(outside, is_master=True, name="Outside")
    (tmp_path / "forest").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        cluster_api.discover(tmp_path)


def test_workshop_parser_ignores_missing_files_and_sorts(tmp_path: Path) -> None:
    first = tmp_path / "one.lua"
    first.write_text(
        "return {\n"
        '    ["workshop-10"] = { enabled = true },\n'
        '    ["workshop-2"] = { enabled = true },\n'
        '    ["workshop-7"] = { enabled = false,\n'
        '        configuration_options = { note = "workshop-99" } },\n'
        "}\n",
        encoding="utf-8",
    )

    assert mods.workshop_ids((first, tmp_path / "missing.lua")) == (2, 10)


def test_console_ensure_replaces_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "console"
    path.touch()

    console.ensure(path)
    console.ensure(path)

    assert stat.S_ISFIFO(path.stat().st_mode)


def test_console_ensure_refuses_directory(tmp_path: Path) -> None:
    path = tmp_path / "console"
    path.mkdir()

    with pytest.raises(IsADirectoryError, match="console path is a directory"):
        console.ensure(path)

    assert path.is_dir()


async def test_prepare_shared_skips_updater_without_mods(
    service_layout: tuple[Path, Path],
) -> None:
    install, cluster = service_layout
    shard = cluster / "forest"
    write_shard(shard, is_master=True, name="Forest")
    (shard / "modoverrides.lua").write_text("return {}", encoding="utf-8")
    mods_path = cluster / "mods"
    mods_path.mkdir()
    (mods_path / "dedicated_server_mods_setup.lua").write_text(
        '-- ServerModCollectionSetup("99")\n',
        encoding="utf-8",
    )

    shards = await service.prepare_shared(install, cluster, update_mods=True)

    assert tuple(value.name for value in shards) == ("forest",)


async def test_activate_shard_and_create_server_config(
    service_layout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, cluster = service_layout
    monkeypatch.delenv("DST_SERVER_CLUSTER_NAME", raising=False)
    write_shard(cluster / "forest", is_master=True, name="Forest")
    write_shard(cluster / "cave", is_master=False, name="Caves")

    shards = await service.prepare_shared(install, cluster, update_mods=False)
    selected = next(value for value in shards if value.name == "cave")
    service.activate_shard(install, cluster, selected)
    config = service.create_server_config(
        install,
        cluster,
        selected,
        external_port=30007,
    )

    assert config.shard == "cave"
    assert config.telemetry_cluster == "dst-000"
    assert config.extra_args == (
        "-skip_update_server_mods",
        "-external_port",
        "30007",
    )
    assert not (cluster / "console").exists()
    assert (cluster / "cave" / "console").is_fifo()


async def test_prepare_shared_updates_collection_only_setup(
    service_layout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, cluster = service_layout
    mods_path = cluster / "mods"
    mods_path.mkdir()
    write_shard(cluster / "forest", is_master=True, name="Forest")
    (mods_path / "dedicated_server_mods_setup.lua").write_text(
        'ServerModCollectionSetup("99")\n',
        encoding="utf-8",
    )
    calls = []

    async def update(  # ruff: ignore[unused-async]
        *args: object,
        **kwargs: object,
    ) -> None:
        assert (install / "mods").is_symlink()
        assert (install / "mods").resolve() == (cluster / "mods").resolve()
        assert args[1] == cluster / "mods" / "ugc"
        calls.append((args, kwargs))

    monkeypatch.setattr(mods, "update", update)

    await service.prepare_shared(install, cluster, update_mods=True)

    assert len(calls) == 1
    assert "lock_path" not in calls[0][1]


async def test_prepare_shared_validates_layout_before_replacing_mods(
    service_layout: tuple[Path, Path],
) -> None:
    install, cluster = service_layout
    install_mods = install / "mods"
    install_mods.mkdir()
    sentinel = install_mods / "keep"
    sentinel.touch()
    (cluster / "forest").mkdir()

    with pytest.raises(FileNotFoundError, match=r"server\.ini"):
        await service.prepare_shared(install, cluster, update_mods=False)

    assert sentinel.is_file()
    assert not (cluster / "mods").exists()
    assert not any(
        (cluster / name).exists()
        for name in ("adminlist.txt", "whitelist.txt", "blocklist.txt")
    )


async def test_mod_updater_uses_isolated_config_ports(tmp_path: Path) -> None:
    executable = tmp_path / "fake-updater"
    executable.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('ARGS|' + '|'.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    ugc = tmp_path / "ugc"
    ugc.mkdir()
    lines: list[str] = []

    await mods.update(
        executable,
        ugc,
        log_handler=lines.append,
    )

    arguments = lines[0].split("|")[1:]
    assert "-only_update_server_mods" in arguments
    assert arguments[arguments.index("-ugc_directory") + 1] == str(ugc)
    ports = {
        int(arguments[arguments.index(name) + 1])
        for name in (
            "-port",
            "-steam_master_server_port",
        )
    }
    assert len(ports) == 2
    assert "-steam_authentication_port" not in arguments
