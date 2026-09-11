# ruff: file-ignore[async-function-with-timeout, print]
"""Game operations and direct RPC access for the host CLI."""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Annotated, Any, Literal

from cyclopts import App, Parameter
from pydantic import JsonValue

from dst_server import commands as c
from dst_server.cli.common import (
    BatchFailure,
    batch,
    context,
    emit,
    make_host,
    select_rooms,
)
from dst_server.host.logs import logs as journal_logs
from dst_server.models.console import ConsoleResult
from dst_server.rpc.client import ClusterClient, RemoteEndpoint, ShardClient
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_RELOAD_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
)

type AllRooms = Annotated[bool, Parameter(name="--all")]
type Operation = Callable[[ClusterClient], Awaitable[Any]]

player_app = App(name="player", help="Inspect players and manage permissions.")
world_app = App(name="world", help="Inspect and operate game worlds.")
console_app = App(name="console", help="Evaluate Lua through a game agent.")
logs_app = App(name="logs", help="Read historical or live journal logs.")
rpc_app = App(name="rpc", help="Discover and call the running server's RPC methods.")


async def _run(
    operation: Operation,
    room: tuple[str, ...],
    all_rooms: bool,
    template: str | None,
    *,
    render: Callable[[Sequence[dict[str, Any]]], None] | None = None,
) -> None:
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)

        async def run(number: int) -> Any:
            async with host.connect(number) as client:
                return await operation(client)

        await batch(numbers, run, render=render)


async def _master(client: ClusterClient, shard: str | None) -> ShardClient:
    return client.shard(shard if shard is not None else (await client.status()).master)


async def _command(
    command: c.Request[Any],
    room: tuple[str, ...],
    all_rooms: bool,
    template: str | None,
    *,
    shard: str | None = None,
    master: bool = False,
) -> None:
    async def invoke(client: ClusterClient) -> Any:
        target = await _master(client, shard) if master or shard is not None else client
        result = await target.invoke(command)
        if isinstance(command, c.ClusterPause):
            failed = tuple(
                item.shard
                for item in result
                if item.error is not None or item.value is not True
            )
            if failed:
                action = "pause" if command.paused else "resume"
                msg = f"{action} was not confirmed by shards: {', '.join(failed)}"
                raise CommandResultError(result, msg)
        elif isinstance(command, c.Pause) and result is not True:
            action = "pause" if command.paused else "resume"
            msg = f"{action} was not confirmed by shard: {shard}"
            raise CommandResultError(result, msg)
        return result

    await _run(invoke, room, all_rooms, template)


async def announce(
    message: str,
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Broadcast a message to every selected room."""
    await _command(c.Announce(message=message), room, all_rooms, template)


@player_app.command(name="list")
async def player_list(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """List players across a room or in a particular shard."""
    await _command(
        c.LocatePlayers() if shard is None else c.ListPlayers(),
        room,
        all_rooms,
        template,
        shard=shard,
    )


@player_app.command(name="show")
async def player_show(
    userid: str,
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Show one player's current state and location."""
    await _command(
        c.LocatePlayer(userid=userid) if shard is None else c.GetPlayer(userid=userid),
        room,
        all_rooms,
        template,
        shard=shard,
    )


async def _player_command(
    command: c.Request[Any],
    userid: str,
    room: tuple[str, ...],
    all_rooms: bool,
    template: str | None,
    shard: str | None,
) -> None:
    async def invoke(client: ClusterClient) -> Any:
        target = shard
        if target is None:
            located = await client.get_player(userid)
            if located is not None:
                if located.shard is None:
                    msg = f"player {userid} is migrating; retry when migration finishes"
                    raise ValueError(msg)
                target = located.shard
        return await (await _master(client, target)).invoke(command)

    await _run(invoke, room, all_rooms, template)


@player_app.command(name="kick")
async def player_kick(
    userid: str,
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Kick a player from their current shard."""
    await _player_command(
        c.Kick(userid=userid), userid, room, all_rooms, template, shard
    )


@player_app.command(name="ban")
async def player_ban(
    userid: str,
    *,
    seconds: int | None = None,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Ban a player, optionally for a specified number of seconds."""
    await _player_command(
        c.Ban(userid=userid, seconds=seconds),
        userid,
        room,
        all_rooms,
        template,
        shard,
    )


@player_app.command(name="unban")
async def player_unban(
    userid: str,
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Remove a player from a running room's blocklist."""
    await _command(
        c.Unban(userid=userid), room, all_rooms, template, shard=shard, master=True
    )


async def _permission(
    kind: Literal["admin", "whitelist", "ban"],
    userid: str | None,
    remove: bool,
    room: tuple[str, ...],
    all_rooms: bool,
    template: str | None,
) -> None:
    if remove and userid is None:
        msg = "--remove requires a user ID"
        raise ValueError(msg)
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)

        async def edit(number: int) -> tuple[str, ...]:
            return await host.permission(number, kind, userid, remove=remove)

        await batch(numbers, edit)


@player_app.command(name="admin")
async def player_admin(
    userid: str | None = None,
    *,
    remove: bool = False,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """List admins, or add/remove an ID in the room's admin file."""
    await _permission("admin", userid, remove, room, all_rooms, template)


@player_app.command(name="whitelist")
async def player_whitelist(
    userid: str | None = None,
    *,
    remove: bool = False,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """List whitelist entries, or add/remove an ID in the whitelist file."""
    await _permission("whitelist", userid, remove, room, all_rooms, template)


@player_app.command(name="blocklist")
async def player_blocklist(
    userid: str | None = None,
    *,
    remove: bool = False,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Read or edit persistent bans, including when a room is offline."""
    await _permission("ban", userid, remove, room, all_rooms, template)


@world_app.command(name="info")
async def world_info(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Inspect a shard's world; defaults to the master shard."""
    await _command(c.World(), room, all_rooms, template, shard=shard, master=True)


@world_app.command(name="save")
async def world_save(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    timeout: float = DEFAULT_SAVE_TIMEOUT,
) -> None:
    """Save every shard, or one explicitly selected shard."""
    await _command(
        c.ClusterSave(timeout=timeout) if shard is None else c.Save(timeout=timeout),
        room,
        all_rooms,
        template,
        shard=shard,
    )


@world_app.command(name="pause")
async def world_pause(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Pause every shard, or one explicitly selected shard."""
    await _command(
        c.ClusterPause(paused=True) if shard is None else c.Pause(paused=True),
        room,
        all_rooms,
        template,
        shard=shard,
    )


@world_app.command(name="resume")
async def world_resume(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Resume every shard, or one explicitly selected shard."""
    await _command(
        c.ClusterPause(paused=False) if shard is None else c.Pause(paused=False),
        room,
        all_rooms,
        template,
        shard=shard,
    )


@world_app.command(name="snapshots")
async def world_snapshots(
    *,
    limit: int = 100,
    before: int | None = None,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """List saved world snapshots."""
    await _command(
        c.Snapshots(limit=limit, before=before), room, all_rooms, template, shard=shard
    )


@world_app.command(name="rollback")
async def world_rollback(
    *,
    count: int = 1,
    day: int | None = None,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    timeout: float = DEFAULT_RELOAD_TIMEOUT,
) -> None:
    """Roll back a whole room by snapshot count or to a specific day."""
    if day is not None and count != 1:
        msg = "--day and --count are mutually exclusive"
        raise ValueError(msg)
    await _command(
        c.Rollback(count=count, timeout=timeout)
        if day is None
        else c.RollbackToDay(day=day, timeout=timeout),
        room,
        all_rooms,
        template,
    )


@world_app.command(name="regenerate")
async def world_regenerate(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    timeout: float = DEFAULT_RELOAD_TIMEOUT,
) -> None:
    """Generate a new world using the room's saved world-generation settings."""
    await _command(
        c.Regenerate(timeout=timeout)
        if shard is None
        else c.RegenerateShard(timeout=timeout),
        room,
        all_rooms,
        template,
        shard=shard,
    )


def _read_input(file: Path) -> str:
    return sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")


def _source(source: str | None, file: Path | None) -> str:
    if source is not None and file is not None:
        msg = "provide Lua source or --file, not both"
        raise ValueError(msg)
    if file is not None:
        source = _read_input(file)
    elif source is None:
        if sys.stdin.isatty():
            msg = "provide Lua source, --file, piped input, or --interactive"
            raise ValueError(msg)
        source = sys.stdin.read()
    if not source.strip():
        msg = "Lua source cannot be empty"
        raise ValueError(msg)
    return source


class CommandResultError(RuntimeError):
    def __init__(self, result: Any, message: str) -> None:
        self.result = result
        super().__init__(message)


def _show_console(result: ConsoleResult) -> None:
    if context().json:
        emit(result)
        return
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    for value in result.values:
        print(value.text)
    if result.error is not None:
        print(f"{result.error.kind}: {result.error.message}", file=sys.stderr)
    if result.truncated:
        print("Console output was truncated by the game agent.", file=sys.stderr)


def _show_console_rooms(results: Sequence[dict[str, Any]]) -> None:
    for record in results:
        print(f"Room {record['room']:03d}")
        result = record.get("result")
        if isinstance(result, ConsoleResult):
            _show_console(result)
        elif not record["ok"]:
            print(record["error"], file=sys.stderr)


async def _console_session(target: ShardClient, timeout: float) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    session: PromptSession[str] = PromptSession()
    with patch_stdout():
        while True:
            try:
                source = await session.prompt_async(f"{target.name}> ")
            except EOFError:
                return
            except KeyboardInterrupt:
                continue
            if source.strip():
                _show_console(await target.evaluate(source, timeout=timeout))


async def _follow_logs(unit: str) -> None:
    async with aclosing(journal_logs((unit,), lines=0, follow=True)) as records:
        async for record in records:
            emit(record if context().json else record.message)


@console_app.default
async def console(
    source: str | None = None,
    *,
    file: Path | None = None,
    interactive: Annotated[bool, Parameter(name=("--interactive", "-i"))] = False,
    follow: bool = False,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> None:
    """Evaluate an expression or Lua statements once; use -i for an input prompt."""
    if follow and not interactive:
        msg = "--follow requires --interactive; use logs --follow for log output alone"
        raise ValueError(msg)
    if not interactive:
        text = _source(source, file)

        async def evaluate(client: ClusterClient) -> ConsoleResult:
            result = await (await _master(client, shard)).evaluate(
                text, timeout=timeout
            )
            if result.error is not None:
                raise CommandResultError(result, result.error.message)
            return result

        await _run(evaluate, room, all_rooms, template, render=_show_console_rooms)
        return
    if source is not None or file is not None:
        msg = "--interactive does not accept an initial source or --file"
        raise ValueError(msg)
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)
        if len(numbers) != 1:
            msg = "interactive console requires exactly one room"
            raise ValueError(msg)
        number = numbers[0]
        async with host.connect(number) as client:
            target = await _master(client, shard)
            if not follow:
                await _console_session(target, timeout)
                return
            async with asyncio.TaskGroup() as tasks:
                task = tasks.create_task(
                    _follow_logs(host.shard_unit(number, target.name))
                )
                try:
                    await _console_session(target, timeout)
                finally:
                    task.cancel()


@logs_app.default
async def logs(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    lines: int = 100,
    follow: bool = False,
    since: str | None = None,
    until: str | None = None,
    after_cursor: str | None = None,
) -> None:
    """Read journal history, including stopped rooms and previous game runs."""
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)
        units = tuple(
            unit
            for number in numbers
            for unit in (
                (host.shard_unit(number, shard),)
                if shard is not None
                else host.units(number)
            )
        )
        async with aclosing(
            journal_logs(
                units,
                lines=lines,
                follow=follow,
                since=since,
                until=until,
                after_cursor=after_cursor,
            )
        ) as records:
            async for record in records:
                emit(
                    record
                    if context().json
                    else (
                        f"{record.timestamp.isoformat()} {record.unit}: "
                        f"{record.message}"
                    )
                )


def _endpoint(client: ClusterClient, shard: str | None) -> RemoteEndpoint:
    return client if shard is None else client.shard(shard)


@rpc_app.command(name="list")
async def rpc_list(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """List methods advertised by the selected running endpoint."""

    async def describe(client: ClusterClient) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "method": item.name,
                "scope": item.scope,
                "mutation": item.mutation,
                "timeout": item.default_timeout,
            }
            for item in await _endpoint(client, shard).describe()
        )

    await _run(describe, room, all_rooms, template)


@rpc_app.command(name="describe")
async def rpc_describe(
    method: str,
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Show one method's arguments, result schema and side-effect semantics."""

    async def describe(client: ClusterClient) -> c.MethodDescription:
        for item in await _endpoint(client, shard).describe():
            if item.name == method:
                return item
        msg = f"RPC method is not advertised by this endpoint: {method}"
        raise ValueError(msg)

    await _run(describe, room, all_rooms, template)


def _arguments(file: Path | None, fields: tuple[str, ...]) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {}
    if file is not None:
        text = _read_input(file)
        c.validate_json_structure(text.encode())
        value = json.loads(text)
        if not isinstance(value, dict):
            msg = "RPC input must be a JSON object"
            raise ValueError(msg)
        arguments.update(value)
    for field in fields:
        name, separator, text = field.partition("=")
        if not separator or not name:
            msg = "RPC fields must have the form name=value"
            raise ValueError(msg)
        if name in arguments:
            msg = f"duplicate RPC argument: {name}"
            raise ValueError(msg)
        try:
            c.validate_json_structure(text.encode())
            value = json.loads(text)
        except json.JSONDecodeError:
            value = text
        arguments[name] = value
    return arguments


@rpc_app.command(name="call")
async def rpc_call(
    method: str,
    *,
    input: Path | None = None,
    field: Annotated[tuple[str, ...], Parameter(name=("--field", "-f"))] = (),
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    timeout: float | None = None,
) -> None:
    """Call a method using --input JSON or -f name=value (JSON or plain text)."""
    arguments = _arguments(input, field)

    async def call(client: ClusterClient) -> JsonValue:
        return await _endpoint(client, shard).call(method, arguments, timeout=timeout)

    await _run(call, room, all_rooms, template)


@rpc_app.command(name="subscribe")
async def rpc_subscribe(
    kind: Literal["logs", "lifecycle", "events"],
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
) -> None:
    """Stream new RPC records until interrupted; journal history is under logs."""
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)

        async def stream(number: int) -> bool:
            try:
                async with host.connect(number) as client:
                    await _stream_records(_endpoint(client, shard), number, kind)
            except Exception as error:
                emit({"room": f"{number:03d}", "ok": False, "error": str(error)})
                return False
            return True

        # Live subscriptions must all stay active, not queue behind a batch limit.
        if not all(await asyncio.gather(*(stream(number) for number in numbers))):
            raise BatchFailure


async def _stream_records(
    target: RemoteEndpoint,
    number: int,
    kind: Literal["logs", "lifecycle", "events"],
) -> None:
    subscribe = {
        "logs": target.subscribe_logs,
        "lifecycle": target.subscribe_lifecycle,
        "events": target.subscribe_events,
    }[kind]
    async with await subscribe() as subscription:
        while not subscription.closed:
            for record in await subscription.next():
                emit({"room": f"{number:03d}", "record": record})
