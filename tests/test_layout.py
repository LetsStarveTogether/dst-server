from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import dst_server
from dst_server.runtime import Server, ServerConfig
from dst_server.steamcmd import SteamCMD

REMOVED_MODULES = (
    "arguments",
    "console",
    "driver",
    "fd_wrapper",
    "game_events",
    "instrumentation",
    "mods",
    "observers",
    "otel",
    "process",
    "protocol",
    "runner",
    "schema",
    "server_events",
    "validation",
)


def test_package_layout_and_root_api() -> None:
    root = Path(dst_server.__file__).parent

    assert {path.name for path in root.glob("*.py")} == {"__init__.py", "steamcmd.py"}
    assert dst_server.__all__ == ["Server", "ServerConfig", "SteamCMD"]
    assert dst_server.Server is Server
    assert dst_server.ServerConfig is ServerConfig
    assert dst_server.SteamCMD is SteamCMD
    assert not hasattr(dst_server, "ServerArgs")
    assert all(find_spec(f"dst_server.{name}") is None for name in REMOVED_MODULES)
