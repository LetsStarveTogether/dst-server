from pathlib import Path

from dst_server.game import GameClient
from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder


def make_game(response: str = "") -> tuple[GameClient, list[str]]:
    commands: list[str] = []

    async def execute(command: str) -> str:  # ruff:ignore[unused-async]
        commands.append(command)
        return response

    async def execute_reload(  # ruff:ignore[unused-async]
        command: str,
        completion_timeout: float,
    ) -> tuple[str, int, float]:
        del completion_timeout
        commands.append(command)
        return response, 0, float("inf")

    async def wait_reload(  # ruff:ignore[unused-async]
        generation: int,
        deadline: float,
    ) -> None:
        del generation, deadline

    game = GameClient(
        shard="Master",
        lua_directory=Path("/lua"),
        telemetry=TelemetrySettings(),
        execute=execute,
        execute_ready=execute,
        execute_reload=execute_reload,
        wait_reload=wait_reload,
        recorder=Recorder("cluster", "Master"),
        session_id=lambda: "SESSION",
        nonce="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    return game, commands
