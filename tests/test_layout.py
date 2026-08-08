from __future__ import annotations

import dst_server
from dst_server.runtime import IndeterminateCommandError, Server, ServerConfig
from dst_server.steamcmd import SteamCMD


def test_root_api() -> None:
    assert set(dst_server.__all__) == {
        "IndeterminateCommandError",
        "ResponseTooLargeError",
        "Server",
        "ServerConfig",
        "SteamCMD",
    }
    assert dst_server.IndeterminateCommandError is IndeterminateCommandError
    assert dst_server.Server is Server
    assert dst_server.ServerConfig is ServerConfig
    assert dst_server.SteamCMD is SteamCMD
