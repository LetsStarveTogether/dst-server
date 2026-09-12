# ruff: file-ignore[async-function-with-timeout]
"""Room operations shared by the CLI, timers and Python callers."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import TypeAdapter

from dst_server.announcements import (
    MOD_UPDATE_NOTICE,
    Countdown,
    Repeat,
)
from dst_server.concurrency import complete
from dst_server.configuration.files import (
    atomic_write,
    configuration_file_exists,
    discover,
    prepare,
    read_text,
    write_files,
)
from dst_server.configuration.models import ShardName, cluster_structure
from dst_server.deployment.application import QuadletApplication, _escape_unit_name
from dst_server.deployment.quadlet import _escape_expansions, references_pod
from dst_server.klei_id import encode_klei_id
from dst_server.logs import (
    JournalLogs,
    JournalQuery,
    JournalResult,
    JournalStream,
    NetdataLogQuery,
    NetdataLogResult,
    NetdataLogs,
)
from dst_server.mods.process import run_process
from dst_server.rooms import (
    DEFAULT_QUADLET_DIR,
    DEFAULT_ROOT,
    Room,
    RoomStore,
    write_control,
)
from dst_server.rpc import ClusterClient, rpc_runtime
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_LIFECYCLE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    positive_timeout,
)

from .locking import RoomBusyError, room_lock
from .schedule import effective_state

logger = logging.getLogger(__name__)
_SHARD_NAME = TypeAdapter(ShardName)
_LOG_CLUSTER = "attributes.dst.cluster.name"
_LOG_SHARD = "attributes.dst.shard.name"
_JOURNAL_QUERY = JournalQuery()
_JOURNAL_FOLLOW = JournalQuery(direction="forward", limit=0)


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


class Host:  # ruff: ignore[too-many-public-methods]
    def __init__(
        self,
        cluster_root: Path = DEFAULT_ROOT,
        quadlet_dir: Path = DEFAULT_QUADLET_DIR,
        *,
        systemd: Any = None,
        journal_logs: JournalLogs | None = None,
        netdata_logs: NetdataLogs | None = None,
    ) -> None:
        self.cluster_root = cluster_root.absolute()
        self.quadlet_dir = quadlet_dir.absolute()
        self.rooms = RoomStore(self.cluster_root, self.quadlet_dir)
        self._systemd = systemd
        self.journal_logs = journal_logs if journal_logs is not None else JournalLogs()
        self.netdata_logs = netdata_logs if netdata_logs is not None else NetdataLogs()
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

    def _log_clusters(self, numbers: int | Sequence[int]) -> tuple[str, ...]:
        selected = (numbers,) if isinstance(numbers, int) else numbers
        if (
            isinstance(selected, (str, bytes))
            or not isinstance(selected, Sequence)
            or not selected
        ):
            message = "log queries require at least one explicit room number"
            raise ValueError(message)
        return tuple(
            dict.fromkeys(f"dst-{self.rooms.path(number).name}" for number in selected)
        )

    def log_units(
        self, numbers: int | Sequence[int], *, shard: str | None = None
    ) -> tuple[str, ...]:
        """Select retained identities, including rooms and shards already removed."""
        clusters = self._log_clusters(numbers)
        if shard is not None:
            name = _escape_unit_name(_SHARD_NAME.validate_python(shard, strict=True))
            return tuple(f"{cluster}-{name}.service" for cluster in clusters)
        # An exact unit keeps a new room's empty journal a valid query.
        return tuple(
            unit
            for cluster in clusters
            for unit in (f"{cluster}-pod.service", f"{cluster}-*.service")
        )

    async def journal(
        self,
        numbers: int | Sequence[int],
        request: JournalQuery = _JOURNAL_QUERY,
        *,
        shard: str | None = None,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> JournalResult:
        """Query retained room output without loading configuration or using RPC."""
        return await self.journal_logs.query(
            self.log_units(numbers, shard=shard),
            request,
            completion_timeout=completion_timeout,
        )

    @asynccontextmanager
    async def follow_journal(
        self,
        numbers: int | Sequence[int],
        request: JournalQuery = _JOURNAL_FOLLOW,
        *,
        shard: str | None = None,
    ) -> AsyncIterator[JournalStream]:
        """Follow in one owned process; unit globs resolve when reading starts."""
        async with self.journal_logs.follow(
            self.log_units(numbers, shard=shard), request
        ) as stream:
            yield stream

    async def telemetry(
        self,
        numbers: int | Sequence[int],
        request: NetdataLogQuery,
        *,
        shard: str | None = None,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> NetdataLogResult:
        """Query a bounded OTel result across the room's retained service names."""
        clusters = self._log_clusters(numbers)
        if any(field in {_LOG_CLUSTER, _LOG_SHARD} for field, _ in request.filters):
            message = "room and shard log filters are managed by Host.telemetry"
            raise ValueError(message)
        filters = tuple((_LOG_CLUSTER, cluster) for cluster in clusters)
        if shard is not None:
            filters += ((_LOG_SHARD, _SHARD_NAME.validate_python(shard, strict=True)),)
        return await self.netdata_logs.query(
            request.replace(filters=(*filters, *request.filters)),
            completion_timeout=completion_timeout,
        )

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
        units = await self.systemd.list_units(self.units(number))
        state = next(
            (unit for unit in units.values() if unit.active == "failed"),
            units.get(self.unit(number)),
        )
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
        result = await self.status(number)
        units = self.units(number)
        result["units"] = await self.systemd.list_units(units)
        result["logs"] = await self.journal(number, JournalQuery(limit=50))
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
            if any((self.quadlet_dir / path).exists() for path in application.files()):
                msg = f"deployment already exists for room {definition.number}"
                raise FileExistsError(msg)
            self.rooms.save(definition)
            prepare(directory)
        return self.rooms.load(definition.number)

    async def edit(self, definition: Room) -> Room:
        """Write configuration for a stopped room without changing its lifecycle."""
        definition = Room.model_validate(definition)
        number = definition.number
        directory = self.rooms.path(number)
        async with room_lock(directory):
            previous = self.rooms.load(number)
            if await self._running_units(number):
                msg = "configuration changes require a stopped room"
                raise RuntimeError(msg)
            structural = self._structure(previous) != self._structure(definition)
            application = definition.application(directory) if structural else None
            if application is not None:
                application.validate_updates(self.quadlet_dir)
            definition.save_game(directory, previous=previous)
            self.rooms.save_policy(definition)
            if application is not None:
                files = application.files()
                pod = f"{application.pod.name}.pod"
                for path in self.quadlet_dir.glob("*.container"):
                    if Path(path.name) not in files and references_pod(path, pod):
                        path.unlink()
                application.save(self.quadlet_dir)
                await self.systemd.reload()
            return self.rooms.load(number)

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

    async def _check_busy(self, number: int) -> None:
        try:
            async with asyncio.timeout(5), self.connect(number) as client:
                busy = (await client.status()).busy
        except OSError, TimeoutError, RuntimeError:
            # An unreachable controller must not prevent an operator recovery.
            return
        if busy:
            msg = f"room operation is busy: {number:03d}"
            raise RoomBusyError(msg)

    async def _transition(  # ruff: ignore[complex-structure, too-many-branches]
        self,
        number: int,
        action: Literal["start", "stop", "restart"],
        *,
        automatic: bool,
        wait: bool,
        timeout: float,
    ) -> dict[str, Any]:
        timeout = positive_timeout(timeout)
        directory = self.rooms.path(number)
        async with asyncio.timeout(timeout):
            async with room_lock(directory, wait=action == "stop" and not automatic):
                policy = self.rooms.policy(number)
                if automatic and effective_state(policy) is not (action != "stop"):
                    return {"number": number, "action": "skipped", "waiting": False}
                running = await self._running_units(number)
                operation = action
                if action != "stop":
                    if running:
                        state = await self.status(number, game=False)
                        if state["active"] == "failed":
                            operation = "restart"
                        else:
                            await self._check_busy(number)
                    await self.systemd.reload()
                    if not automatic:
                        for unit in self.units(number):
                            await self.systemd.reset_failed(unit)
                if not automatic:
                    write_control(
                        directory,
                        policy.model_copy(update={"paused": action == "stop"}),
                    )
                if action == "stop":
                    for unit in running:
                        await self.systemd.stop(unit)
                else:
                    await getattr(self.systemd, operation)(self.unit(number))
            if wait:
                await self.systemd.wait_idle(self.units(number), timeout)
                if action != "stop":
                    return await self.wait_ready(number, timeout=timeout)
                if await self._running_units(number):
                    msg = "room services did not stop"
                    raise RuntimeError(msg)
            return {"number": number, "action": action, "waiting": not wait}

    async def start(
        self,
        number: int,
        *,
        automatic: bool = False,
        wait: bool = True,
        timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
    ) -> dict[str, Any]:
        return await self._transition(
            number, "start", automatic=automatic, wait=wait, timeout=timeout
        )

    async def stop(
        self,
        number: int,
        *,
        automatic: bool = False,
        wait: bool = True,
        timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
    ) -> dict[str, Any]:
        return await self._transition(
            number, "stop", automatic=automatic, wait=wait, timeout=timeout
        )

    async def restart(
        self,
        number: int,
        *,
        automatic: bool = False,
        wait: bool = True,
        timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,
    ) -> dict[str, Any]:
        return await self._transition(
            number, "restart", automatic=automatic, wait=wait, timeout=timeout
        )

    async def wait_ready(
        self,
        number: int,
        *,
        timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> dict[str, Any]:
        async with asyncio.timeout(positive_timeout(timeout)):
            while True:
                state = await self.status(number)
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

    async def announce(self, number: int, message: str | Repeat | Countdown) -> None:
        async with self.connect(number) as client:
            await client.announce(message)

    async def update_mods(
        self,
        number: int,
        *,
        restart: bool = False,
        notice: Countdown | None = MOD_UPDATE_NOTICE,
    ) -> None:
        notice = Countdown.model_validate(notice) if notice is not None else None
        directory = self.rooms.path(number)
        async with room_lock(directory):
            policy = self.rooms.policy(number)
            running = await self._running_units(number)
            if running:
                if not restart:
                    msg = "MOD updates require --restart while the room is running"
                    raise RuntimeError(msg)
            else:
                write_control(directory, policy.model_copy(update={"paused": True}))
                await self._prepare_mods(number)
                return
        async with self.connect(number) as client:
            await client.update_mods(restart=True, notice=notice)

    async def _prepare_mods(self, number: int) -> None:
        application = QuadletApplication.load(
            self.quadlet_dir, name=f"dst-{number:03d}"
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
