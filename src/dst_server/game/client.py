from __future__ import annotations

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
    json_text,
    lua_package_path,
    lua_request,
    lua_string,
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
        recorder: Recorder,
        session_id: Callable[[], str | None],
        nonce: str,
    ) -> None:
        self.shard = shard
        self.lua_directory = lua_directory
        self.telemetry = telemetry
        self.execute = execute
        self.recorder = recorder
        self.session_id = session_id
        self.nonce = nonce
        self.health: DriverHealth | None = None
        self.world = WorldClient(self)
        self.players = PlayerClient(self)

    @property
    def driver_health(self) -> DriverHealth:
        if self.health is None:
            msg = "DST Lua driver has not been installed"
            raise RuntimeError(msg)
        return self.health

    async def install(self) -> DriverHealth:
        options = self.telemetry.model_dump(mode="json") | {
            "nonce": self.nonce,
        }
        package_path = lua_package_path(self.lua_directory)
        body = (
            f"package.path={lua_string(package_path)}..package.path;"
            f"local driver=require({lua_string(DRIVER_MODULE)});"
            "return driver.install("
            f"json.decode({lua_string(json_text(options))}))"
        )
        self.health = await self.send(body, DRIVER_RESPONSE)
        return self.health

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
                f"json.decode({lua_string(json_text(arguments))}))"
            )
            return await self.send(body, adapter)

    async def send[DataT](
        self,
        body: str,
        adapter: ResponseAdapter[DataT],
    ) -> DataT:
        result = await self.execute(lua_request(body))
        response = next(
            (
                line.removeprefix(RESULT_PREFIX)
                for line in reversed(result.splitlines())
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
        self.health = await self.request("health", {}, DRIVER_RESPONSE)
        return self.health


__all__ = ["GameClient"]
