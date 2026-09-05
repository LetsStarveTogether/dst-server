from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import JsonValue

from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder

from .players import PlayerClient
from .rpc import (
    DRIVER_RESPONSE,
    RESULT_PREFIX,
    DriverHealth,
    Failure,
    ResponseAdapter,
    lua_package_path,
    lua_request,
    lua_string,
    lua_value,
)
from .world import WorldClient

DRIVER_MODULE = "dst_server"


class GameClient:
    def __init__(
        self,
        *,
        shard: str,
        lua_directory: Path,
        telemetry: TelemetrySettings,
        execute: Callable[[str], Awaitable[str]],
        execute_ready: Callable[[str], Awaitable[str]],
        recorder: Recorder,
        session_id: Callable[[], str | None],
        nonce: str,
        execute_reload: Callable[[str, float], Awaitable[tuple[str, int, float]]],
        wait_reload: Callable[[int, float], Awaitable[None]],
        observe_health: Callable[[int, DriverHealth], None] | None = None,
    ) -> None:
        self.shard = shard
        self.lua_directory = lua_directory
        self.telemetry = telemetry
        self.execute = execute
        self.execute_ready = execute_ready
        self.execute_reload = execute_reload
        self.wait_reload = wait_reload
        self.observe_health = observe_health
        self.recorder = recorder
        self.session_id = session_id
        self.nonce = nonce
        self.world = WorldClient(self)
        self.players = PlayerClient(self)

    async def install(self, generation: int) -> DriverHealth:
        options = self.telemetry.model_dump(mode="json") | {
            "nonce": self.nonce,
            "generation": generation,
        }
        package_path = lua_package_path(self.lua_directory)
        body = (
            f"local driver=require({lua_string(DRIVER_MODULE)});"
            f"return driver.install({lua_value(options)})"
        )
        result = await self.execute(
            f"local path={lua_string(package_path)};"
            "if not (';'..package.path..';'):find(';'..path,1,true) then "
            "package.path=path..package.path end;" + lua_request(body)
        )
        return self.parse(result, DRIVER_RESPONSE)

    async def request[DataT](
        self,
        method: str,
        arguments: dict[str, JsonValue],
        adapter: ResponseAdapter[DataT],
    ) -> DataT:
        with self.recorder.operation(
            f"lua.{method}",
            self.session_id(),
        ) as span:
            span.set_attribute("dst.lua.method", method)
            body = (
                f"return require({lua_string(DRIVER_MODULE)}).call("
                f"{lua_string(method)},"
                f"{lua_value(arguments)})"
            )
            result = await self.execute_ready(lua_request(body))
            return self.parse(result, adapter)

    async def reload[DataT](
        self,
        method: str,
        arguments: dict[str, JsonValue],
        adapter: ResponseAdapter[DataT],
        completion_timeout: float,
    ) -> DataT:
        with self.recorder.operation(
            f"lua.{method}",
            self.session_id(),
        ) as span:
            span.set_attribute("dst.lua.method", method)
            body = (
                f"return require({lua_string(DRIVER_MODULE)}).call("
                f"{lua_string(method)},"
                f"{lua_value(arguments)})"
            )
            result, generation, deadline = await self.execute_reload(
                lua_request(body), completion_timeout
            )
            data = self.parse(result, adapter)
            await self.wait_reload(generation, deadline)
            return data

    def parse[DataT](
        self,
        result: str,
        adapter: ResponseAdapter[DataT],
    ) -> DataT:
        response = next(
            (
                line.removeprefix(RESULT_PREFIX)
                for line in reversed(result.split("\n"))
                if line.startswith(RESULT_PREFIX)
            ),
            None,
        )
        if response is None:
            msg = "DST command did not return a structured result"
            raise RuntimeError(msg)
        envelope = adapter.validate_json(response, strict=True)
        if isinstance(envelope, Failure):
            msg = f"DST Lua request failed: {envelope.error}"
            raise RuntimeError(msg)  # ruff:ignore[type-check-without-type-error]
        return envelope.data

    async def get_health(self) -> DriverHealth:
        health = await self.request("health", {}, DRIVER_RESPONSE)
        if self.observe_health is not None:
            self.observe_health(health.generation, health)
        return health
