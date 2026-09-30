"""Prepare a fresh, credential-free cluster for the native Rust P0 probe."""

# The command uses explicit trusted paths; this helper reports its artifacts.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print]

import argparse
import secrets
import subprocess
from pathlib import Path


def prepare(directory: Path, shards: int) -> Path:
    # A new directory prevents accidentally probing existing native saves.
    directory.mkdir(parents=True, exist_ok=False)
    cluster = directory / "cluster"
    cluster.mkdir()
    (cluster / "cluster.ini").write_text(
        "[SHARD]\n"
        f"shard_enabled = {'true' if shards > 1 else 'false'}\n"
        "master_ip = 127.0.0.1\nmaster_port = 10888\n"
        f"cluster_key = {secrets.token_hex(24)}\n\n"
        "[NETWORK]\n"
        f"cluster_name = Rust P0: {shards} worlds\n"
        "offline_cluster = true\nlan_only_cluster = true\n\n"
        "[GAMEPLAY]\nmax_players = 64\n"
    )
    for filename in (
        "cluster_token.txt",
        "adminlist.txt",
        "whitelist.txt",
        "blocklist.txt",
    ):
        (cluster / filename).touch()
    mods = cluster / "mods"
    mods.mkdir()
    (mods / "modsettings.lua").touch()
    (mods / "dedicated_server_mods_setup.lua").touch()
    for index in range(shards):
        name = "surface" if index == 0 else f"world-{index + 1}"
        shard = cluster / name
        shard.mkdir()
        (shard / "server.ini").write_text(
            f"[SHARD]\nname = {name}\nis_master = {'true' if index == 0 else 'false'}\n"
            f"id = {index + 1}\n\n[NETWORK]\nserver_port = {10999 + index}\n\n"
            f"[STEAM]\nmaster_server_port = {27018 + index}\n\n"
            "[ACCOUNT]\nencode_user_path = true\n"
        )
        preset = "DST_CAVE" if index == 1 else "SURVIVAL_TOGETHER"
        (shard / "worldgenoverride.lua").write_text(
            "return { override_enabled = true, "
            f'worldgen_preset = "{preset}", settings_preset = "{preset}", '
            'overrides = { world_size = "small" } }\n'
        )
        (shard / "modoverrides.lua").write_text("return {}\n")
    (cluster / ".dst-rust-probe").touch()
    return cluster


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--shards", type=int, choices=(1, 2, 5), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=Path("target/debug/dst-server"))
    args = parser.parse_args()
    cluster = prepare(args.directory, args.shards)
    bundle = args.directory / "scripts.zip"
    subprocess.run(
        [
            str(args.binary.resolve()),
            "scripts",
            "build",
            str(args.source),
            "--output",
            str(bundle),
        ],
        check=True,
    )
    print(f"{cluster}\n{bundle}")


if __name__ == "__main__":
    main()
