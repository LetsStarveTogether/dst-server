import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import JsonValue

from dst_server import commands as c
from dst_server.api import EndpointAPI, PlayerAPI
from dst_server.errors import IndeterminateCommandError
from dst_server.events.server import SavedEvent
from dst_server.json_codec import validate_json_structure
from dst_server.models.driver import DriverHealth
from dst_server.telemetry.recorder import Recorder
from dst_server.timeouts import DEFAULT_RELOAD_TIMEOUT

from .rpc import (
    MAX_RESULT_LINE_BYTES,
    SAVE_RESPONSE,
    Failure,
    LuaRequestError,
    ResponseAdapter,
    response_adapter,
)

_MUTATIONS = {
    spec.request.method for spec in c.OPERATIONS if spec.game and spec.mutation
} | {"save"}


class GameClient(EndpointAPI):
    def __init__(
        self,
        *,
        shard: str,
        execute_ready: Callable[[str, dict[str, JsonValue]], Awaitable[bytes]],
        recorder: Recorder,
        session_id: Callable[[], str | None],
        execute_reload: Callable[
            [str, dict[str, JsonValue], float], Awaitable[tuple[bytes, int, float]]
        ],
        wait_reload: Callable[[int, float], Awaitable[None]],
        observe_health: Callable[[int, DriverHealth], None] | None = None,
    ) -> None:
        self.shard = shard
        self.execute_ready = execute_ready
        self.execute_reload = execute_reload
        self.wait_reload = wait_reload
        self.observe_health = observe_health
        self.recorder = recorder
        self.session_id = session_id
        self.players = PlayerAPI(self)

    async def invoke[T](self, command: c.Request[T]) -> T:
        spec = c.operation("agent", command)
        if isinstance(command, c.IsAdmin):
            player = await self.invoke(
                c.GetPlayer(userid=command.userid, timeout=command.timeout)
            )
            return cast("T", None if player is None else player.admin)
        if spec.game is None:
            msg = f"command {command.method!r} is not available to the game"
            raise ValueError(msg)
        method = command.method
        arguments = command.model_dump(
            mode="json", exclude={"timeout"}, exclude_none=True
        )
        if isinstance(command, c.ConnectedShards):
            arguments["current_name"] = self.shard
        adapter = response_adapter(
            bool if spec.result_type is None else spec.result_type
        )
        try:
            async with asyncio.timeout(command.timeout):
                value = (
                    await self.reload(method, arguments, adapter, command.timeout)
                    if spec.game == "reload"
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

    async def request_save(self) -> SavedEvent:
        result = await self.request("save", {}, SAVE_RESPONSE)
        msg = "DST save callback did not identify a completed snapshot"
        try:
            event = SavedEvent.from_path(result.snapshot)
        except ValueError as error:
            raise IndeterminateCommandError(msg) from error
        if event.snapshot is None:
            raise IndeterminateCommandError(msg)
        return event

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
            result = await self.execute_ready(method, arguments)
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
            result, generation, deadline = await self.execute_reload(
                method, arguments, completion_timeout
            )
            data = self._parse_result(method, result, adapter)
            try:
                await self.wait_reload(generation, deadline)
            except Exception as error:
                msg = "DST reload completion could not be confirmed"
                raise IndeterminateCommandError(msg) from error
            return data

    def _parse_result[T](
        self, method: str, result: bytes, adapter: ResponseAdapter[T]
    ) -> T:
        try:
            return self.parse(result, adapter)
        except (ValueError, RuntimeError) as error:
            if method not in _MUTATIONS or isinstance(error, IndeterminateCommandError):
                raise
            msg = "DST mutation result could not be confirmed"
            raise IndeterminateCommandError(msg) from error

    def parse[DataT](
        self,
        payload: bytes,
        adapter: ResponseAdapter[DataT],
    ) -> DataT:
        if len(payload) > MAX_RESULT_LINE_BYTES:
            msg = "DST command result exceeds the line size limit"
            raise RuntimeError(msg)
        validate_json_structure(payload)
        envelope = adapter.validate_json(payload, strict=True)
        if isinstance(envelope, Failure):
            if envelope.error == "indeterminate":
                msg = "DST mutation may have been applied"
                raise IndeterminateCommandError(msg)
            raise LuaRequestError(envelope.error)
        return envelope.data
