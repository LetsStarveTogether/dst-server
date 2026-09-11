# ruff: file-ignore[async-function-with-timeout]
"""Room operations shared by the CLI, timers and Python callers."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from dst_server.concurrency import complete
from dst_server.configuration.files import (
    atomic_write,
    configuration_file_exists,
    discover,
    prepare,
    read_text,
    write_files,
)
from dst_server.configuration.models import cluster_structure
from dst_server.deployment.application import QuadletApplication, _escape_unit_name
from dst_server.deployment.quadlet import _escape_expansions, references_pod
from dst_server.klei_id import encode_klei_id
from dst_server.mods.process import run_process
from dst_server.rooms import (
    DEFAULT_QUADLET_DIR,
    DEFAULT_ROOT,
    Control,
    Room,
    RoomStore,
    control_revision,
)
from dst_server.rpc import ClusterClient, rpc_runtime
from dst_server.timeouts import (
    DEFAULT_LIFECYCLE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    positive_timeout,
)

from .locking import MOD_LOCK, room_lock
from .schedule import (
    effective_state,
    record_transition,
)

logger = logging.getLogger(__name__)


def _preparation_options(
    arguments: tuple[str, ...], names: set[str]
) -> tuple[str, ...]:
    result = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        flag, separator, _ = argument.partition("=")
        if flag in names:
            result.append(argument)
            if not separator:
                index += 1
                if index == len(arguments):
                    msg = f"missing generated container argument for {flag}"
                    raise ValueError(msg)
                result.append(arguments[index])
        index += 1
    return tuple(result)


class Host:
    def __init__(
        self,
        cluster_root: Path = DEFAULT_ROOT,
        quadlet_dir: Path = DEFAULT_QUADLET_DIR,
        *,
        systemd: Any = None,
    ) -> None:
        self.cluster_root = cluster_root.absolute()
        self.quadlet_dir = quadlet_dir.absolute()
        self.rooms = RoomStore(self.cluster_root, self.quadlet_dir)
        self._systemd = systemd
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> Self:
        await self._stack.enter_async_context(rpc_runtime())
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            if self._systemd is not None:
                await self._systemd.aclose()
        finally:
            await self._stack.aclose()

    @property
    def systemd(self) -> Any:
        if self._systemd is None:
            try:
                from .systemd import Systemd
            except ImportError as error:
                msg = "host operations require dst-server[host]"
                raise RuntimeError(msg) from error
            self._systemd = Systemd()
        return self._systemd

    @staticmethod
    def unit(number: int) -> str:
        return f"dst-{number:03d}-pod.service"

    def units(self, number: int) -> tuple[str, ...]:
        """Read deployed units even when the native game configuration is damaged."""
        self.rooms.path(number)
        pod = f"dst-{number:03d}.pod"
        return (
            self.unit(number),
            *(
                f"{path.stem}.service"
                for path in sorted(
                    self.quadlet_dir.glob(f"dst-{number:03d}-*.container")
                )
                if references_pod(path, pod)
            ),
        )

    def shard_unit(self, number: int, shard: str) -> str:
        unit = f"dst-{number:03d}-{_escape_unit_name(shard)}.service"
        if unit not in self.units(number):
            msg = f"unknown deployed shard: {shard}"
            raise ValueError(msg)
        return unit

    @asynccontextmanager
    async def connect(self, number: int) -> AsyncIterator[ClusterClient]:
        async with await ClusterClient.connect(
            self.rooms.path(number) / ".dst-server.sock"
        ) as client:
            yield client

    async def status(self, number: int, *, game: bool = True) -> dict[str, Any]:
        self.rooms.path(number)
        configuration_error = None
        try:
            self.rooms.policy(number)
            discover(self.rooms.path(number))
        except OSError, ValueError:
            configuration_error = "room configuration could not be loaded"
        units = await self.systemd.list_units((self.unit(number),))
        state = units.get(self.unit(number))
        result: dict[str, Any] = {
            "number": number,
            "load": state.load if state else "not-found",
            "active": state.active if state else "inactive",
            "sub": state.sub if state else "dead",
            "job_id": state.job_id if state else 0,
            "job_type": state.job_type if state else "",
            "game": None,
            "error": configuration_error,
            "configuration_error": configuration_error,
        }
        if game and result["active"] == "active":
            try:
                async with asyncio.timeout(5), self.connect(number) as client:
                    result["game"] = await client.status()
            except (OSError, TimeoutError, RuntimeError) as error:
                result["error"] = str(error)
        return result

    async def diagnose(self, number: int) -> dict[str, Any]:
        from .logs import logs

        result = await self.status(number)
        units = self.units(number)
        result["units"] = await self.systemd.list_units(units)
        result["logs"] = [record async for record in logs(units, lines=50)]
        return result

    async def create(self, definition: Room) -> Room:
        definition = Room.model_validate(definition)
        directory = self.rooms.path(definition.number)
        application = definition.application(directory)
        definition.game_files()
        application.files()
        async with room_lock(directory):
            if any(path.name != ".dst-operation.lock" for path in directory.iterdir()):
                msg = f"room already exists: {directory}"
                raise FileExistsError(msg)
            async with room_lock(self.quadlet_dir):
                if any(
                    (self.quadlet_dir / path).exists() for path in application.files()
                ):
                    msg = f"deployment already exists for room {definition.number}"
                    raise FileExistsError(msg)
                self.rooms.save(definition)
                prepare(directory)
        return self.rooms.load(definition.number)

    async def edit(  # ruff: ignore[complex-structure]
        self,
        definition: Room,
        *,
        restart: bool = False,
        expected: Room | None = None,
    ) -> Room:
        definition = Room.model_validate(definition)
        number = definition.number
        directory = self.rooms.path(number)
        async with (
            room_lock(directory),
            room_lock(directory, name=MOD_LOCK, wait=False),
        ):
            previous = self.rooms.load(number)
            self._check_definition(previous, expected)
            game_changed = definition.game_files() != previous.game_files()
            structural = self._structure(previous) != self._structure(definition)
            if not game_changed and not structural and not restart:
                self.rooms.save_policy(definition)
                return definition
            running = await self._running_units(number)
            if running and not restart:
                msg = "game and deployment changes require a stopped room or --restart"
                raise RuntimeError(msg)
            if structural:
                self._deployment(previous, definition).validate_updates(
                    self.quadlet_dir
                )
            revision = record_transition(directory, previous, False, override=False)
            for unit in running:
                await self.systemd.stop(unit)
        await self.systemd.wait_idle(running, DEFAULT_LIFECYCLE_TIMEOUT)
        async with (
            room_lock(directory),
            room_lock(directory, name=MOD_LOCK, wait=False),
        ):
            self._check_revision(number, revision)
            self._check_definition(
                self.rooms.load(number), previous, updated=definition
            )
            if await self._running_units(number):
                msg = "room services are still running; configuration was not changed"
                raise RuntimeError(msg)
            async with room_lock(self.quadlet_dir):
                application = (
                    self._deployment(previous, definition) if structural else None
                )
                if application is not None:
                    application.validate_updates(self.quadlet_dir)
                definition.save_game(directory, previous=previous)
                self.rooms.save_policy(definition)
                revision = control_revision(directory)
                if application is not None:
                    files = application.files()
                    pod = f"{application.pod.name}.pod"
                    for path in self.quadlet_dir.glob("*.container"):
                        if Path(path.name) not in files and references_pod(path, pod):
                            path.unlink()
                    application.save(self.quadlet_dir)
                    await self.systemd.reload()
        if restart:
            await self.start(number, expected_revision=revision)
        return self.rooms.load(number)

    def _deployment(self, previous: Room, updated: Room) -> QuadletApplication:
        directory = self.rooms.path(updated.number)
        actual = QuadletApplication.load(
            self.quadlet_dir, name=f"dst-{updated.number:03d}", legacy=True
        )
        application = actual.patch(
            previous.application(directory), updated.application(directory)
        )
        pod = f"{application.pod.name}.pod"
        for path in application.files():
            target = self.quadlet_dir / path
            if (
                path.suffix == ".container"
                and configuration_file_exists(target)
                and not references_pod(target, pod)
            ):
                msg = f"refusing to replace an unrelated unit: {target}"
                raise ValueError(msg)
        return application

    @staticmethod
    def _check_definition(
        current: Room, expected: Room | None, *, updated: Room | None = None
    ) -> None:
        if expected is None:
            return
        before = expected.game_files()
        actual = current.game_files()
        after = updated.game_files() if updated is not None else actual
        paths = before.keys() | after.keys()
        if updated is not None:
            paths = {path for path in paths if before.get(path) != after.get(path)}
        if (
            any(actual.get(path) != before.get(path) for path in paths)
            or (
                (updated is None or updated.deployment != expected.deployment)
                and current.deployment != expected.deployment
            )
            or any(
                getattr(current, field) != getattr(expected, field)
                for field in ("number", "template", "schedule", "recycle")
            )
        ):
            msg = "room configuration changed; read it again before editing"
            raise RuntimeError(msg)

    async def _running_units(self, number: int) -> tuple[str, ...]:
        states = await self.systemd.list_units(self.units(number))
        return tuple(
            unit
            for unit, state in states.items()
            if state.active not in {"inactive", "failed"} or state.job_id
        )

    @staticmethod
    def _structure(definition: Room) -> object:
        return (
            definition.deployment,
            cluster_structure(
                definition.cluster.settings,
                {
                    name: shard.settings
                    for name, shard in definition.cluster.shards.items()
                },
            ),
        )

    async def _transition(
        self,
        number: int,
        action: Literal["start", "stop", "restart"],
        *,
        override: bool,
        wait: bool,
        timeout: float,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        timeout = positive_timeout(timeout)
        directory = self.rooms.path(number)
        async with asyncio.timeout(timeout):
            if action == "restart":
                async with (
                    room_lock(directory),
                    room_lock(directory, name=MOD_LOCK, wait=False),
                ):
                    definition = self.rooms.policy(number)
                    self._check_revision(number, expected_revision)
                    self._check_schedule(number, definition, action, override=override)
                    expected_revision = record_transition(
                        directory, definition, True, override=override
                    )
                    running = await self._running_units(number)
                    for unit in running:
                        await self.systemd.stop(unit)
                await self.systemd.wait_idle(running, timeout)
            async with room_lock(directory):
                definition = self.rooms.policy(number)
                self._check_revision(number, expected_revision)
                self._check_schedule(number, definition, action, override=override)
                if action != "stop":
                    async with (
                        room_lock(directory, name=MOD_LOCK, wait=False),
                        room_lock(self.quadlet_dir),
                    ):
                        running = await self._running_units(number)
                        if action == "restart" and running:
                            msg = "room did not stop"
                            raise RuntimeError(msg)
                        await self.systemd.reload()
                        self._check_schedule(
                            number, definition, action, override=override
                        )
                        revision = record_transition(
                            directory, definition, True, override=override
                        )
                        await self.systemd.start(self.unit(number))
                else:
                    revision = record_transition(
                        directory, definition, False, override=override
                    )
                    for unit in await self._running_units(number):
                        await self.systemd.stop(unit)
            if wait:
                await self.systemd.wait_idle(self.units(number), timeout)
                self._check_revision(number, revision)
                if action != "stop":
                    return await self.wait_ready(
                        number, timeout=timeout, expected_revision=revision
                    )
                if await self._running_units(number):
                    msg = "room services did not stop"
                    raise RuntimeError(msg)
            return {"number": number, "action": action, "waiting": not wait}

    def _check_schedule(
        self, number: int, definition: Control, action: str, *, override: bool
    ) -> None:
        if override or not definition.schedule:
            return
        desired = effective_state(self.rooms.path(number), definition)
        if (action == "restart" and desired is False) or (
            action != "restart" and desired is not (action == "start")
        ):
            msg = "room operation was superseded"
            raise RuntimeError(msg)

    def _check_revision(self, number: int, expected: int | None) -> None:
        if (
            expected is not None
            and control_revision(self.rooms.path(number)) != expected
        ):
            msg = "room operation was superseded"
            raise RuntimeError(msg)

    async def start(
        self,
        number: int,
        *,
        override: bool = True,
        wait: bool = True,
        timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self._transition(
            number,
            "start",
            override=override,
            wait=wait,
            timeout=timeout,
            expected_revision=expected_revision,
        )

    async def stop(
        self,
        number: int,
        *,
        override: bool = True,
        wait: bool = True,
        timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self._transition(
            number,
            "stop",
            override=override,
            wait=wait,
            timeout=timeout,
            expected_revision=expected_revision,
        )

    async def restart(
        self,
        number: int,
        *,
        override: bool = True,
        wait: bool = True,
        timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self._transition(
            number,
            "restart",
            override=override,
            wait=wait,
            timeout=timeout,
            expected_revision=expected_revision,
        )

    async def wait_ready(
        self,
        number: int,
        *,
        timeout: float = DEFAULT_STARTUP_TIMEOUT,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        async with asyncio.timeout(positive_timeout(timeout)):
            while True:
                self._check_revision(number, expected_revision)
                state = await self.status(number)
                self._check_revision(number, expected_revision)
                game = state["game"]
                if (
                    game is not None
                    and game.phase == "running"
                    and not game.missing_shards
                    and all(shard.ready for shard in game.shards)
                ):
                    return state
                if (
                    state["load"] != "loaded"
                    or state["active"] in {"failed", "inactive", "deactivating"}
                    or (game is not None and game.phase == "failed")
                ):
                    detail = state["error"] or getattr(game, "error", None)
                    msg = f"room {number:03d} failed to start: {detail}"
                    raise RuntimeError(msg)
                await asyncio.sleep(1)

    async def announce(self, number: int, message: str) -> None:
        async with self.connect(number) as client:
            await client.announce(message)

    async def update_mods(self, number: int, *, restart: bool = False) -> None:
        directory = self.rooms.path(number)
        async with (
            room_lock(directory),
            room_lock(directory, name=MOD_LOCK, wait=False),
        ):
            policy = self.rooms.policy(number)
            running = await self._running_units(number)
            if running and not restart:
                msg = "MOD updates require --restart while the room is running"
                raise RuntimeError(msg)
            revision = record_transition(directory, policy, False, override=False)
            for unit in running:
                await self.systemd.stop(unit)
        await self.systemd.wait_idle(running, DEFAULT_LIFECYCLE_TIMEOUT)
        async with room_lock(directory, name=MOD_LOCK, wait=False):
            async with room_lock(directory):
                self._check_revision(number, revision)
                if await self._running_units(number):
                    msg = "room services are still running; MODs were not updated"
                    raise RuntimeError(msg)
            await self._prepare_mods(number)
        if running:
            await self.start(number, expected_revision=revision)

    async def _prepare_mods(self, number: int) -> None:
        application = QuadletApplication.load(
            self.quadlet_dir, name=f"dst-{number:03d}", legacy=True
        )
        await self.systemd.reload()
        arguments = await self.systemd.exec_start(f"{application.master.name}.service")
        pod_arguments = await self.systemd.exec_start(self.unit(number), pre=True)
        if (
            arguments[1:2] != ("run",)
            or Path(arguments[0]).name != "podman"
            or pod_arguments[1:3] != ("pod", "create")
            or Path(pod_arguments[0]).name != "podman"
        ):
            msg = "MOD preparation requires generated Podman container and pod commands"
            raise ValueError(msg)
        entrypoint = next(
            (
                index
                for index in range(len(arguments) - 1, 1, -1)
                if arguments[index : index + 3]
                == ("/app/.venv/bin/dst-server", "agent", "master")
                or arguments[index : index + 2]
                == ("/app/.venv/bin/dst-server", "master")
            ),
            None,
        )
        if entrypoint is None:
            msg = "MOD preparation requires the DST master container command"
            raise ValueError(msg)
        name = f"dst-mod-update-{uuid4().hex}"
        command = ["podman", "run", "--rm", "--name", name, "--pull=never"]
        command.extend(_preparation_options(pod_arguments[3:], {"--userns"}))
        # ponytail: copy preparation inputs; extend the flag list when more are needed.
        command.extend(
            _preparation_options(
                arguments[2 : entrypoint - 1],
                {"--env", "--env-file", "-v", "--volume", "--mount"},
            )
        )
        command.extend((
            arguments[entrypoint - 1],
            "/app/.venv/bin/dst-server",
            "agent",
            "prepare",
        ))
        environment = dict(os.environ)
        try:
            code = await run_process(
                *command, cwd=None, environment=environment, on_line=logger.info
            )
            if code:
                msg = f"MOD preparation failed with exit code {code}"
                raise RuntimeError(msg)
        finally:
            code = await complete(
                run_process(
                    "podman",
                    "rm",
                    "--force",
                    "--ignore",
                    name,
                    cwd=None,
                    environment=environment,
                    on_line=logger.info,
                )
            )
            if code:
                msg = f"MOD preparation container cleanup failed with exit code {code}"
                raise RuntimeError(msg)

    async def permission(
        self,
        number: int,
        kind: Literal["admin", "whitelist", "ban"],
        userid: str | None = None,
        *,
        remove: bool = False,
    ) -> tuple[str, ...]:
        names = {
            "admin": "adminlist.txt",
            "whitelist": "whitelist.txt",
            "ban": "blocklist.txt",
        }
        if kind not in names:
            raise ValueError(kind)
        if userid is not None:
            encode_klei_id(userid)
        async with room_lock(self.rooms.path(number)):
            self.rooms.policy(number)
            path = self.rooms.path(number) / names[kind]
            running = await self._running_units(number) if kind != "admin" else ()
            if running:
                async with self.connect(number) as client:
                    if kind == "ban":
                        master = next(
                            shard.name
                            for shard in discover(self.rooms.path(number))
                            if shard.master
                        )
                        players = client.shard(master).players
                        if userid is not None:
                            await players.unban(
                                userid
                            ) if remove else await players.ban(userid)
                        return await players.blocklist()
                    if userid is not None:
                        changed = (
                            await client.unwhitelist(userid)
                            if remove
                            else await client.whitelist(userid)
                        )
                        if not changed:
                            msg = "game did not confirm the whitelist change"
                            raise RuntimeError(msg)
            existing = (
                read_text(path).splitlines() if configuration_file_exists(path) else []
            )
            if userid is not None:
                existing = [item for item in existing if item != userid]
                if not remove:
                    existing.append(userid)
                atomic_write(path, "".join(f"{item}\n" for item in existing), 0o600)
            return tuple(existing)

    async def install_automation(
        self, *, unit_dir: Path = Path("/etc/systemd/system")
    ) -> tuple[Path, ...]:
        import sys
        from importlib.resources import files

        source = files("dst_server.host").joinpath("systemd")
        replacements = {
            "@PYTHON@": sys.executable,
            "@ROOT@": str(self.cluster_root),
            "@QUADLET@": str(self.quadlet_dir),
        }
        if any(
            any(char in value for char in "\0\n\r") for value in replacements.values()
        ):
            msg = "automation paths cannot contain NUL or newlines"
            raise ValueError(msg)
        contents = {
            Path(item.name): item.read_text()
            for item in source.iterdir()
            if item.name.endswith((".service", ".timer"))
        }
        for path, template in contents.items():
            content = template
            for placeholder, value in replacements.items():
                argument = '"' + _escape_expansions(value).replace('"', '\\"') + '"'
                content = content.replace(placeholder, argument)
            contents[path] = content
        written = write_files(unit_dir, contents)
        await self.systemd.reload()
        return written
