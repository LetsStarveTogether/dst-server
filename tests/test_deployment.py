from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from dst_server import (
    discover_shards,
    ensure_fifo,
    prepare_cluster,
    prepare_mods,
    update_server_mods,
    workshop_mod_ids,
)
from dst_server.runner import prepare_servers


def write_shard(path: Path, *, is_master: bool, name: str) -> None:
    path.mkdir()
    (path / "server.ini").write_text(
        f"[SHARD]\nis_master = {str(is_master).lower()}\nname = {name}\nid = 1\n",
        encoding="utf-8",
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
        'return { ["workshop-42"] = {}, ["workshop-7"] = {} }',
        encoding="utf-8",
    )
    (cluster / "cave" / "modoverrides.lua").write_text(
        'return { ["workshop-42"] = {} }',
        encoding="utf-8",
    )

    prepare_cluster(cluster)
    mod_ids = prepare_mods(install, cluster)
    shards = discover_shards(cluster)

    assert mod_ids == (7, 42)
    assert install_mods.is_symlink()
    assert install_mods.resolve() == (cluster / "mods").resolve()
    assert (cluster / "mods" / "ugc").is_dir()
    assert (cluster / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == 'ServerModSetup("7")\nServerModSetup("42")\n'
    assert {shard.name for shard in shards} == {"forest", "cave"}
    assert next(shard for shard in shards if shard.config.is_master).console_path == (
        cluster / "console"
    )
    assert all(
        (cluster / name).is_file()
        for name in ("adminlist.txt", "blocklist.txt", "whitelist.txt")
    )


def test_prepare_cluster_preserves_existing_permission_files(tmp_path: Path) -> None:
    for name in ("cluster.ini", "cluster_token.txt"):
        (tmp_path / name).touch()
    paths = tuple(
        tmp_path / name for name in ("adminlist.txt", "blocklist.txt", "whitelist.txt")
    )
    for path in paths:
        path.touch()
        os.utime(path, ns=(0, 0))

    prepare_cluster(tmp_path)

    assert all(path.stat().st_mtime_ns == 0 for path in paths)


def test_discovery_requires_one_master(tmp_path: Path) -> None:
    write_shard(tmp_path / "one", is_master=False, name="One")
    write_shard(tmp_path / "two", is_master=False, name="Two")

    with pytest.raises(ValueError, match="exactly one master"):
        discover_shards(tmp_path)


def test_workshop_parser_ignores_missing_files_and_sorts(tmp_path: Path) -> None:
    first = tmp_path / "one.lua"
    first.write_text("workshop-10 workshop-2 workshop-10", encoding="utf-8")

    assert workshop_mod_ids((first, tmp_path / "missing.lua")) == (2, 10)


def test_ensure_fifo_replaces_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "console"
    path.touch()

    ensure_fifo(path)
    ensure_fifo(path)

    assert stat.S_ISFIFO(path.stat().st_mode)


async def test_prepare_servers_skips_updater_without_mods(tmp_path: Path) -> None:
    install = tmp_path / "install"
    executable = install / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)
    cluster = tmp_path / "cluster"
    cluster.mkdir()
    for name in ("cluster.ini", "cluster_token.txt"):
        (cluster / name).touch()
    shard = cluster / "forest"
    write_shard(shard, is_master=True, name="Forest")
    (shard / "modoverrides.lua").write_text("return {}", encoding="utf-8")

    shards, servers = await prepare_servers(install, cluster, update_mods=True)

    assert len(shards) == len(servers) == 1


async def test_mod_updater_uses_isolated_config_ports_and_proxy(tmp_path: Path) -> None:
    executable = tmp_path / "fake-updater"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "print('ARGS|' + '|'.join(sys.argv[1:]))\n"
        "print('PROXY|' + os.environ.get('HTTPS_PROXY', ''))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    ugc = tmp_path / "ugc"
    ugc.mkdir()
    lines: list[str] = []

    await update_server_mods(
        executable,
        ugc,
        proxy_url="socks5://127.0.0.1:1080",
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
            "-steam_authentication_port",
        )
    }
    assert len(ports) == 3
    assert lines[1] == "PROXY|socks5://127.0.0.1:1080"
    assert os.environ.get("HTTPS_PROXY") != "socks5://127.0.0.1:1080"
