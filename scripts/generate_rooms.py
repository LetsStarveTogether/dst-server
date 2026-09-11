"""Existing deployment entrypoint; new deployments use dst-server deployment."""

import os
from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr

from dst_server.deployment import DEFAULT_IMAGE
from dst_server.presets.lst import (
    ROOM_NUMBERS,
    TOKEN_ENVIRONMENT,
    generate_rooms,
)


def main(argv: Sequence[str] | None = None) -> None:
    parser = ArgumentParser(description="Generate DST room and Quadlet files.")
    parser.add_argument("rooms", nargs="*", type=int, metavar="ROOM")
    parser.add_argument("--cluster-root", required=True, type=Path)
    parser.add_argument("--quadlet-dir", required=True, type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--volume-idmap")
    parser.add_argument("--userns")
    arguments = parser.parse_args(argv)
    if arguments.token_file is None:
        token = os.environ.get(TOKEN_ENVIRONMENT)
        if token is None:
            parser.error(f"set {TOKEN_ENVIRONMENT} or pass --token-file")
        source = TOKEN_ENVIRONMENT
    else:
        token = arguments.token_file.read_text(encoding="utf-8")
        token = token.removesuffix("\n").removesuffix("\r")
        source = str(arguments.token_file)
    if not token:
        parser.error(f"token is empty: {source}")
    generate_rooms(
        arguments.rooms or ROOM_NUMBERS,
        token=SecretStr(token),
        cluster_root=arguments.cluster_root.resolve(),
        quadlet_dir=arguments.quadlet_dir.resolve(),
        image=arguments.image,
        volume_idmap=arguments.volume_idmap,
        userns=arguments.userns,
    )


if __name__ == "__main__":
    main()
