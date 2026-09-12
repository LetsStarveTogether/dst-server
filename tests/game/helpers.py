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
    ) -> tuple[bytes, int, float]:
        del completion_timeout
        commands.append((method, arguments))
        return response, 0, float("inf")

    async def wait_reload(  # ruff:ignore[unused-async]
        generation: int,
        deadline: float,
    ) -> None:
        del generation, deadline

    game = GameClient(
        shard="Master",
        execute_ready=execute,
        execute_reload=execute_reload,
        wait_reload=wait_reload,
        recorder=Recorder("cluster", "Master"),
        session_id=lambda: "SESSION",
    )
    return game, commands
