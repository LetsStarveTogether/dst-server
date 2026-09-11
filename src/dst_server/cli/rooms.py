# ruff: file-ignore[async-function-with-timeout]
"""Room configuration and deployment commands backed by the host SDK."""

import json
import os
from pathlib import Path
from typing import Annotated, Any

from cyclopts import App, Parameter
from pydantic import SecretStr

from dst_server.configuration.models import ClusterSettings, _shared_cluster_key
from dst_server.configuration.overrides import ModOverride
from dst_server.deployment import DEFAULT_IMAGE
from dst_server.presets.lst import (
    ROOM_NUMBERS,
    TOKEN_ENVIRONMENT,
    build_template,
    fleet_room,
    template_names,
)
from dst_server.rooms import Room, RoomDeployment
from dst_server.timeouts import DEFAULT_LIFECYCLE_TIMEOUT, DEFAULT_STARTUP_TIMEOUT

from .common import batch, emit, make_host, parse_rooms, select_rooms

room_app = App(name="room", help="Create, configure, and run rooms.")
template_app = App(
    name="template", help="Inspect and explicitly apply gameplay templates."
)
deployment_app = App(name="deployment", help="Install the LST fleet and automation.")
mod_app = App(name="mod", help="Configure and update room mods.")

type AllRooms = Annotated[bool, Parameter(name="--all")]
type RoomOptions = Annotated[tuple[str, ...], Parameter(name="--room")]


def _token(path: Path | None) -> SecretStr:
    value = path.read_text().strip() if path else os.environ.get(TOKEN_ENVIRONMENT, "")
    if not value:
        msg = f"set {TOKEN_ENVIRONMENT} or pass --token-file with a non-empty token"
        raise ValueError(msg)
    return SecretStr(value)


def _settings(
    name: str | None,
    max_players: int | None,
    password: str | None,
    description: str | None,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "cluster_name": name,
            "max_players": max_players,
            "cluster_password": SecretStr(password) if password is not None else None,
            "cluster_description": description,
        }.items()
        if value is not None
    }


def _summary(definition: Room) -> dict[str, Any]:
    return {
        "number": definition.number,
        "name": definition.cluster.settings.cluster_name,
        "template": definition.template,
        "max_players": definition.cluster.settings.max_players,
        "shards": tuple(definition.cluster.shards),
    }


@room_app.command(name="list")
async def list_rooms(*, template: str | None = None) -> None:
    """List native room configurations without requiring a running game."""
    async with make_host() as host:
        emit([
            _summary(definition)
            for definition in host.rooms.list()
            if template is None or definition.template == template
        ])


@room_app.command(name="show")
async def show_room(number: int, *, field: str | None = None) -> None:
    """Read native room configuration, or one field addressed by JSON Pointer."""
    async with make_host() as host:
        definition = host.rooms.load(number)
        emit(definition.get(field) if field is not None else definition)


@room_app.command(name="schema")
def room_schema() -> None:
    """Show the JSON schema for every configurable room field."""
    emit(Room.model_json_schema())


@room_app.command(name="create")
async def create_room(
    number: int,
    *,
    template: str,
    token_file: Path | None = None,
    name: str | None = None,
    max_players: int | None = None,
    password: str | None = None,
    description: str | None = None,
    image: str = DEFAULT_IMAGE,
    volume_idmap: str | None = None,
    userns: str | None = None,
) -> None:
    """Create one room from a template; refuse to overwrite an existing room."""
    definition = Room(
        number=number,
        template=template,
        cluster=build_template(
            template,
            number=number,
            token=_token(token_file),
            settings=ClusterSettings(
                **_settings(name, max_players, password, description)
            ),
        ),
        deployment=RoomDeployment(
            image=image, volume_idmap=volume_idmap, userns=userns
        ),
    )
    async with make_host() as host:
        emit(_summary(await host.create(definition)))


@room_app.command(name="edit")
async def edit_rooms(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
    name: str | None = None,
    max_players: int | None = None,
    password: str | None = None,
    description: str | None = None,
    set_fields: Annotated[tuple[str, ...], Parameter(name="--set")] = (),
    unset_fields: Annotated[tuple[str, ...], Parameter(name="--unset")] = (),
    restart: bool = False,
) -> None:
    """Edit native settings; running rooms require --restart.

    --set accepts /json/pointer=JSON; --unset removes an explicitly set field.
    Common settings are applied first, followed by --set and then --unset.

    Raises:
        ValueError: No changes were supplied or a --set assignment is malformed.
    """
    settings = _settings(name, max_players, password, description)
    changes = [
        (
            f"/cluster/settings/{name}",
            value.get_secret_value() if isinstance(value, SecretStr) else value,
        )
        for name, value in settings.items()
    ]
    for assignment in set_fields:
        pointer, separator, value = assignment.partition("=")
        if not separator:
            msg = "--set requires /json/pointer=JSON"
            raise ValueError(msg)
        changes.append((pointer, json.loads(value)))
    if not (changes or unset_fields):
        msg = "provide a setting, --set, or --unset"
        raise ValueError(msg)
    async with make_host() as host:

        async def edit(number: int) -> dict[str, Any]:
            original = host.rooms.load(number)
            definition = original.edit_many(changes, unset=unset_fields)
            return _summary(
                await host.edit(definition, restart=restart, expected=original)
            )

        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template), edit
        )


@room_app.command(name="start")
async def start_rooms(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
    wait: bool = True,
    timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
) -> None:
    """Start selected rooms and, by default, wait for game readiness."""
    async with make_host() as host:
        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template),
            lambda number: host.start(number, wait=wait, timeout=timeout),
        )


@room_app.command(name="stop")
async def stop_rooms(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
    wait: bool = True,
    timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
) -> None:
    """Stop selected rooms, overriding schedules until their next boundary."""
    async with make_host() as host:
        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template),
            lambda number: host.stop(number, wait=wait, timeout=timeout),
        )


@room_app.command(name="restart")
async def restart_rooms(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
    wait: bool = True,
    timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
) -> None:
    """Restart selected rooms and wait for game readiness."""
    async with make_host() as host:
        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template),
            lambda number: host.restart(number, wait=wait, timeout=timeout),
        )


@room_app.command(name="status")
async def room_status(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Query service and game status for selected rooms."""
    async with make_host() as host:
        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template),
            host.status,
        )


@room_app.command(name="wait")
async def wait_rooms(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
    timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> None:
    """Wait until every selected room's game shards are ready."""
    async with make_host() as host:
        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template),
            lambda number: host.wait_ready(number, timeout=timeout),
        )


@room_app.command(name="diagnose")
async def diagnose_rooms(
    rooms: tuple[str, ...] = (),
    *,
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Collect configuration and service diagnostics for selected rooms."""
    async with make_host() as host:
        await batch(
            select_rooms(host, rooms, all_rooms=all_rooms, template=template),
            host.diagnose,
        )


@template_app.command(name="list")
def list_templates() -> None:
    """List the packaged gameplay templates."""
    emit(template_names())


@template_app.command(name="show")
def show_template(name: str) -> None:
    """Show the gameplay configuration produced by a template."""
    emit(build_template(name, token=SecretStr(""), cluster_key=SecretStr("template")))


@template_app.command(name="apply")
async def apply_template(
    name: str,
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    restart: bool = False,
) -> None:
    """Overwrite gameplay configuration, preserving identity and deployment settings."""
    async with make_host() as host:

        async def apply(number: int) -> dict[str, Any]:
            definition = host.rooms.load(number)
            cluster = build_template(
                name,
                number=number,
                token=definition.cluster.token,
                cluster_key=_shared_cluster_key(
                    definition.cluster.settings,
                    (shard.settings for shard in definition.cluster.shards.values()),
                ),
                settings=ClusterSettings(
                    cluster_name=definition.cluster.settings.cluster_name,
                    cluster_description=definition.cluster.settings.cluster_description,
                    cluster_password=definition.cluster.settings.cluster_password,
                ),
            )
            return _summary(
                await host.edit(
                    definition.replace(template=name, cluster=cluster),
                    restart=restart,
                    expected=definition,
                )
            )

        await batch(select_rooms(host, room, all_rooms=all_rooms), apply)


@deployment_app.command(name="lst")
async def deploy_lst(
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    token_file: Path | None = None,
    image: str = DEFAULT_IMAGE,
    volume_idmap: str | None = None,
    userns: str | None = None,
) -> None:
    """Create explicitly selected LST preset rooms (000-139), without overwriting."""
    if all_rooms and room:
        msg = "choose --room or --all, not both"
        raise ValueError(msg)
    numbers = ROOM_NUMBERS if all_rooms else parse_rooms(room)
    if not numbers:
        msg = "select rooms using --room or --all"
        raise ValueError(msg)
    token = _token(token_file)
    definitions = {
        number: fleet_room(
            number, token=token, image=image, volume_idmap=volume_idmap, userns=userns
        )
        for number in numbers
    }
    async with make_host() as host:

        async def create(number: int) -> dict[str, Any]:
            return _summary(await host.create(definitions[number]))

        await batch(numbers, create)


@deployment_app.command(name="install")
async def install_automation() -> None:
    """Install the packaged scheduling and maintenance systemd units."""
    async with make_host() as host:
        emit(await host.install_automation())


@mod_app.command(name="list")
async def list_mods(
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Show each shard's saved mod configuration, including disabled mods."""
    async with make_host() as host:
        emit({
            f"{number:03d}": {
                name: config.mods
                for name, config in host.rooms.load(number).cluster.shards.items()
            }
            for number in select_rooms(
                host, room, all_rooms=all_rooms, template=template
            )
        })


async def _edit_mod(
    mod: str,
    *,
    changes: dict[str, Any],
    room: tuple[str, ...],
    all_rooms: bool,
    template: str | None,
    shard: tuple[str, ...],
    restart: bool,
) -> None:
    name = f"workshop-{mod}" if mod.isdecimal() else mod
    async with make_host() as host:

        async def edit(number: int) -> dict[str, Any]:
            definition = host.rooms.load(number)
            shards = dict(definition.cluster.shards)
            selected = shard or tuple(shards)
            for shard_name in selected:
                config = shards[shard_name]
                entries = dict(config.mods.entries)
                entries[name] = entries.get(name, ModOverride()).replace(**changes)
                shards[shard_name] = config.replace(
                    mods=config.mods.replace(entries=entries)
                )
            return _summary(
                await host.edit(
                    definition.replace(
                        cluster=definition.cluster.replace(shards=shards)
                    ),
                    restart=restart,
                    expected=definition,
                )
            )

        await batch(
            select_rooms(host, room, all_rooms=all_rooms, template=template), edit
        )


@mod_app.command(name="enable")
async def enable_mod(
    mod: str,
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: tuple[str, ...] = (),
    restart: bool = False,
) -> None:
    """Enable a mod in selected shards, or all shards when --shard is omitted."""
    await _edit_mod(
        mod,
        changes={"enabled": True},
        room=room,
        all_rooms=all_rooms,
        template=template,
        shard=shard,
        restart=restart,
    )


@mod_app.command(name="disable")
async def disable_mod(
    mod: str,
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: tuple[str, ...] = (),
    restart: bool = False,
) -> None:
    """Disable a mod, preserving its saved options."""
    await _edit_mod(
        mod,
        changes={"enabled": False},
        room=room,
        all_rooms=all_rooms,
        template=template,
        shard=shard,
        restart=restart,
    )


@mod_app.command(name="set")
async def set_mod(
    mod: str,
    options: str,
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: tuple[str, ...] = (),
    restart: bool = False,
) -> None:
    """Replace a mod's configuration options with a JSON object."""
    parsed = json.loads(options)
    if not isinstance(parsed, dict):
        msg = "mod options must be a JSON object"
        raise TypeError(msg)
    await _edit_mod(
        mod,
        changes={"configuration_options": parsed},
        room=room,
        all_rooms=all_rooms,
        template=template,
        shard=shard,
        restart=restart,
    )


@mod_app.command(name="update")
async def update_mods(
    *,
    room: RoomOptions = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    restart: bool = False,
) -> None:
    """Update downloaded mods; use --restart for running rooms."""
    async with make_host() as host:
        await batch(
            select_rooms(host, room, all_rooms=all_rooms, template=template),
            lambda number: host.update_mods(number, restart=restart),
        )
