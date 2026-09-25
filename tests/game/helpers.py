from pydantic import JsonValue

from dst_server.game import GameClient
from dst_server.telemetry.recorder import Recorder


def make_game(
    response: bytes = b"",
) -> tuple[GameClient, list[tuple[str, dict[str, JsonValue]]]]:
    commands: list[tuple[str, dict[str, JsonValue]]] = []

    async def execute(  # ruff:ignore[unused-async]
        method: str, arguments: dict[str, JsonValue]
    ) -> bytes:
        commands.append((method, arguments))
        return response

    async def execute_reload(  # ruff:ignore[unused-async]
        method: str,
        arguments: dict[str, JsonValue],
        completion_timeout: float,
    ) -> bytes:
        del completion_timeout
        commands.append((method, arguments))
        return response

    game = GameClient(
        shard="Master",
        execute_ready=execute,
        execute_reload=execute_reload,
        recorder=Recorder("cluster", "Master"),
        session_id=lambda: "SESSION",
    )
    return game, commands
