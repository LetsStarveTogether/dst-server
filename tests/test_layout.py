import dst_server
from dst_server.klei_id import decode_klei_id, encode_klei_id
from dst_server.runtime import IndeterminateCommandError, Server, ServerConfig
from dst_server.steamcmd import SteamCMD


def test_root_api() -> None:
    assert set(dst_server.__all__) == {
        "decode_klei_id",
        "encode_klei_id",
        "IndeterminateCommandError",
        "ResponseTooLargeError",
        "Server",
        "ServerConfig",
        "SteamCMD",
    }
    assert dst_server.decode_klei_id is decode_klei_id
    assert dst_server.encode_klei_id is encode_klei_id
    assert dst_server.IndeterminateCommandError is IndeterminateCommandError
    assert dst_server.Server is Server
    assert dst_server.ServerConfig is ServerConfig
    assert dst_server.SteamCMD is SteamCMD
