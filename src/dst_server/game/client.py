import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from dst_server import commands as c
from dst_server.api import EndpointAPI, PlayerAPI
from dst_server.errors import IndeterminateCommandError
from dst_server.lua_codec import lua_string, lua_value
from dst_server.models.driver import DriverHealth
from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder
from dst_server.timeouts import DEFAULT_RELOAD_TIMEOUT

from .rpc import (
    BOOL_RESPONSE,
    DRIVER_RESPONSE,
    MAX_RESULT_LINE_BYTES,
    RESULT_PREFIX,
    Failure,
    LuaRequestError,
    ResponseAdapter,
    lua_package_path,
    lua_request,
    response_adapter,
)

DRIVER_MODULE = "dst_server"


_METHODS: dict[type[c.Request[Any]], str] = {
    c.Health: "health",
    c.Room: "get_room",
    c.World: "get_world",
    c.Runtime: "get_runtime",
    c.Snapshots: "get_snapshots",
    c.Mods: "get_mods",
    c.ConnectedShards: "get_shards",
    c.ListPlayers: "get_players",
    c.GetPlayer: "get_player",
    c.Inventory: "get_player_inventory",
    c.Announce: "announce",
    c.Pause: "set_server_paused",
    c.Reset: "reset",
    c.Regenerate: "regenerate_world",
    c.RegenerateShard: "regenerate_shard",
    c.Rollback: "rollback",
    c.RollbackToSnapshot: "rollback_to_snapshot",
    c.Kick: "kick_player",
    c.Ban: "ban_player",
    c.Blocklist: "get_blocklist",
    c.IsBlocked: "is_blocked",
    c.Unban: "unban_player",
    c.IsWhitelisted: "is_whitelisted",
    c.Whitelist: "whitelist_player",
    c.Unwhitelist: "unwhitelist_player",
    c.SetVitals: "set_player_vitals",
    c.KillPlayer: "kill_player",
    c.Revive: "revive_player",
    c.Despawn: "despawn_player",
    c.Migrate: "migrate_player",
    c.Teleport: "teleport_player",
    c.Give: "give_item",
    c.Remove: "remove_item",
    c.ExecuteJson: "execute_script",
}
_RELOADS = {c.Reset, c.Regenerate, c.RegenerateShard, c.Rollback, c.RollbackToSnapshot}
_MUTATIONS = {
    method
    for command, method in _METHODS.items()
    if c.operation("agent", command.method).mutation
} | {"save"}


class GameClient(EndpointAPI):
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
        self.players = PlayerAPI(self)

    async def invoke[T](self, command: c.Request[T]) -> T:
        spec = c.operation("agent", command)
        if isinstance(command, c.IsAdmin):
            player = await self.invoke(
                c.GetPlayer(userid=command.userid, timeout=command.timeout)
            )
            return cast("T", None if player is None else player.admin)
        method = _METHODS.get(type(command))
        if method is None:
            msg = f"command {command.method!r} is not available to the game"
            raise ValueError(msg)
        arguments = command.model_dump(
            mode="json", exclude={"timeout"}, exclude_none=True
        )
        if isinstance(command, c.Give | c.Remove):
            arguments["prefab"] = arguments.pop("item").lower()
        elif isinstance(command, c.ConnectedShards):
            arguments["current_name"] = self.shard
        adapter = response_adapter(
            bool if spec.result_type is None else spec.result_type
        )
        try:
            async with asyncio.timeout(command.timeout):
                value = (
                    await self.reload(method, arguments, adapter, command.timeout)
                    if type(command) in _RELOADS
                    else await self.request(method, arguments, adapter)
                )
        except TimeoutError as error:
            if spec.mutation:
                msg = "DST mutation result could not be confirmed"
                raise IndeterminateCommandError(msg) from error
            raise
        if isinstance(command, c.Room):
            self.recorder.set_player_count(value.player_count)
        elif isinstance(command, c.ListPlayers):
            self.recorder.set_player_count(len(value))
        elif isinstance(command, c.Health) and self.observe_health is not None:
            self.observe_health(value.generation, value)
        return cast("T", None if spec.result_type is None else value)

    async def request_save(self) -> None:
        await self.request("save", {}, BOOL_RESPONSE)

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
            return self._parse_result(method, result, adapter)

    async def reload[DataT](
        self,
        method: str,
        arguments: dict[str, JsonValue],
        adapter: ResponseAdapter[DataT],
        completion_timeout: float = DEFAULT_RELOAD_TIMEOUT,
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
            data = self._parse_result(method, result, adapter)
            try:
                await self.wait_reload(generation, deadline)
            except Exception as error:
                msg = "DST reload completion could not be confirmed"
                raise IndeterminateCommandError(msg) from error
            return data

    def _parse_result[T](
        self, method: str, result: str, adapter: ResponseAdapter[T]
    ) -> T:
        try:
            return self.parse(result, adapter)
        except (ValueError, RuntimeError) as error:
            safe_failure = (
                isinstance(error, LuaRequestError)
                and error.code == "lua_error"
                and method != "execute_script"
            )
            if (
                method not in _MUTATIONS
                or safe_failure
                or isinstance(error, IndeterminateCommandError)
            ):
                raise
            msg = "DST mutation result could not be confirmed"
            raise IndeterminateCommandError(msg) from error

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
        payload = response.encode()
        if len(payload) + len(RESULT_PREFIX) > MAX_RESULT_LINE_BYTES:
            msg = "DST command result exceeds the line size limit"
            raise RuntimeError(msg)
        c.validate_json_structure(payload)
        envelope = adapter.validate_json(payload, strict=True)
        if isinstance(envelope, Failure):
            if envelope.error == "indeterminate":
                msg = "DST mutation may have been applied"
                raise IndeterminateCommandError(msg)
            raise LuaRequestError(envelope.error)
        return envelope.data

    async def get_health(self) -> DriverHealth:
        return await self.invoke(c.Health())
