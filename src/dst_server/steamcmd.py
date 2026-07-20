from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

type LogHandler = Callable[[str], None]
type SteamCMDCommand = Sequence[str]

COMMAND_NAME = re.compile(r"[A-Za-z0-9_@.-]+\Z")
PLATFORMS = {"linux", "windows", "macos"}
TERMINATE_GRACE_PERIOD = 5.0


class SteamCMD:  # ruff:ignore[too-many-public-methods]
    def __init__(
        self,
        executable: str | Path,
        *,
        steam_home: str | Path | None = None,
        username: str = "anonymous",
        log_handler: LogHandler | None = None,
    ) -> None:
        executable_value = os.fspath(executable)
        if not executable_value:
            msg = "SteamCMD executable must not be empty"
            raise ValueError(msg)
        self.executable = executable_value
        self.steam_home = (
            None if steam_home is None else absolute_path("SteamCMD home", steam_home)
        )
        self.username = validate_argument("username", username)
        self.log_handler = log_handler
        self.execution_lock = asyncio.Lock()

    async def execute(
        self,
        commands: Iterable[SteamCMDCommand],
        *,
        secrets: Collection[str] = (),
    ) -> str:
        command_list = normalize_commands(commands)
        secret_values = normalize_secrets(secrets)
        async with self.execution_lock:
            script = self.write_script(command_list)
            try:
                return await self.execute_script(script, secret_values)
            finally:
                script.unlink(missing_ok=True)

    async def app_info(
        self,
        app_id: int,
        *,
        section: str | None = None,
        refresh: bool = True,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        command = ["app_info_print", str(app_id)]
        if section is not None:
            command.append(validate_argument("app info section", section))
        commands: list[SteamCMDCommand] = []
        if refresh:
            commands.append(("app_info_update", "1"))
        commands.append(tuple(command))
        return await self.execute_authenticated(commands)

    async def depot_info(
        self,
        app_id: int,
        *,
        refresh: bool = True,
    ) -> str:
        return await self.app_info(
            app_id,
            section="depots",
            refresh=refresh,
        )

    async def app_status(
        self,
        app_id: int,
        *,
        install_dir: str | Path | None = None,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        return await self.execute_authenticated(
            [("app_status", str(app_id))],
            install_dir=install_dir,
        )

    async def app_update_status(
        self,
        app_id: int | None = None,
        *,
        install_dir: str | Path | None = None,
    ) -> str:
        command = ["app_update_status"]
        if app_id is not None:
            command.append(str(positive_integer("app ID", app_id)))
        return await self.execute_authenticated(
            [tuple(command)],
            install_dir=install_dir,
        )

    async def app_installed_files(
        self,
        app_id: int,
        *,
        install_dir: str | Path | None = None,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        return await self.execute_authenticated(
            [("app_installed_files", str(app_id))],
            install_dir=install_dir,
        )

    async def apps_installed(self) -> str:
        return await self.execute_authenticated([("apps_installed",)])

    async def package_info(
        self,
        package_id: int,
    ) -> str:
        package_id = positive_integer("package ID", package_id)
        return await self.execute_authenticated(
            [("package_info_print", str(package_id))],
        )

    async def dlc_status(
        self,
        app_id: int,
        dlc_id: int,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        dlc_id = positive_integer("DLC ID", dlc_id)
        return await self.execute_authenticated(
            [("app_dlc_status", str(app_id), str(dlc_id))],
        )

    async def workshop_status(
        self,
        app_id: int,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        return await self.execute_authenticated(
            [("workshop_status", str(app_id))],
        )

    async def update(
        self,
        app_id: int,
        install_dir: str | Path,
        *,
        validate: bool = False,
        language: str | None = None,
        beta: str | None = None,
        beta_password: str | None = None,
        platform: str | None = None,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        command = ["app_update", str(app_id)]
        if beta is not None:
            command.extend(("-beta", validate_argument("beta", beta)))
        if beta_password is not None:
            if beta is None:
                msg = "beta password requires a beta branch"
                raise ValueError(msg)
            command.extend((
                "-betapassword",
                validate_argument("beta password", beta_password),
            ))
        if language is not None:
            command.extend(("-language", validate_argument("language", language)))
        if validate:
            command.append("validate")
        secrets = () if beta_password is None else (beta_password,)
        return await self.execute_authenticated(
            [tuple(command)],
            install_dir=install_dir,
            platform=platform,
            secrets=secrets,
        )

    async def validate(
        self,
        app_id: int,
        install_dir: str | Path,
        *,
        language: str | None = None,
        beta: str | None = None,
        beta_password: str | None = None,
        platform: str | None = None,
    ) -> str:
        return await self.update(
            app_id,
            install_dir,
            validate=True,
            language=language,
            beta=beta,
            beta_password=beta_password,
            platform=platform,
        )

    async def download_depot(
        self,
        app_id: int,
        depot_id: int,
        *,
        manifest_id: int | None = None,
        delta_manifest_id: int | None = None,
        destination: str | Path | None = None,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        depot_id = positive_integer("depot ID", depot_id)
        command = ["download_depot", str(app_id), str(depot_id)]
        if manifest_id is not None:
            command.append(str(positive_integer("manifest ID", manifest_id)))
        if delta_manifest_id is not None:
            if manifest_id is None:
                msg = "delta manifest ID requires a target manifest ID"
                raise ValueError(msg)
            command.append(
                str(positive_integer("delta manifest ID", delta_manifest_id))
            )
        if destination is not None:
            if delta_manifest_id is None:
                msg = "depot destination requires both manifest IDs"
                raise ValueError(msg)
            command.append(str(absolute_path("depot destination", destination)))
        return await self.execute_authenticated([tuple(command)])

    async def workshop_download_item(
        self,
        app_id: int,
        published_file_id: int,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        published_file_id = positive_integer(
            "published file ID",
            published_file_id,
        )
        return await self.execute_authenticated(
            [
                (
                    "workshop_download_item",
                    str(app_id),
                    str(published_file_id),
                )
            ],
        )

    async def uninstall(
        self,
        app_id: int,
        install_dir: str | Path,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        return await self.execute_authenticated(
            [("app_uninstall", str(app_id))],
            install_dir=install_dir,
        )

    async def backup_app(
        self,
        app_id: int,
        target_dir: str | Path,
        *,
        max_folder_size_mb: int | None = None,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        command = [
            "app_backup",
            str(app_id),
            str(absolute_path("backup target", target_dir)),
        ]
        if max_folder_size_mb is not None:
            command.append(
                str(positive_integer("maximum backup folder size", max_folder_size_mb))
            )
        return await self.execute_authenticated([tuple(command)])

    async def request_license(
        self,
        app_id: int,
    ) -> str:
        app_id = positive_integer("app ID", app_id)
        return await self.execute_authenticated(
            [("app_license_request", str(app_id))],
        )

    async def execute_authenticated(
        self,
        commands: Iterable[SteamCMDCommand],
        *,
        install_dir: str | Path | None = None,
        platform: str | None = None,
        secrets: Collection[str] = (),
    ) -> str:
        command_list: list[SteamCMDCommand] = []
        if platform is not None:
            if platform not in PLATFORMS:
                msg = "platform must be linux, windows, or macos"
                raise ValueError(msg)
            command_list.append(("@sSteamCmdForcePlatformType", platform))
        if install_dir is not None:
            command_list.append((
                "force_install_dir",
                str(absolute_path("install directory", install_dir)),
            ))
        command_list.append(("login", self.username))
        command_list.extend(commands)
        return await self.execute(
            command_list,
            secrets=secrets,
        )

    def write_script(self, commands: tuple[tuple[str, ...], ...]) -> Path:
        if self.steam_home is not None:
            self.steam_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".dst-server-steamcmd-",
            suffix=".txt",
            dir=self.steam_home,
            delete=False,
        ) as stream:
            stream.write("@ShutdownOnFailedCommand 1\n")
            stream.write("@NoPromptForPassword 1\n")
            for command in commands:
                stream.write(command[0])
                for argument in command[1:]:
                    stream.write(f" {quote_argument(argument)}")
                stream.write("\n")
            stream.write("quit\n")
            path = Path(stream.name)
        path.chmod(0o600)
        return path

    async def execute_script(
        self,
        script: Path,
        secrets: tuple[str, ...],
    ) -> str:
        environment = None
        working_directory = None
        if self.steam_home is not None:
            environment = os.environ | {"HOME": str(self.steam_home)}
            working_directory = self.steam_home
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "+runscript",
            str(script),
            cwd=working_directory,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024,
            start_new_session=True,
        )
        try:
            output = await self.read_output(process, secrets)
        except BaseException:
            await terminate_process(process)
            raise
        returncode = await process.wait()
        if returncode:
            detail = output[-4000:].strip()
            suffix = "" if not detail else f": {detail}"
            msg = f"SteamCMD exited with status {returncode}{suffix}"
            raise ChildProcessError(msg)
        return output

    async def read_output(
        self,
        process: asyncio.subprocess.Process,
        secrets: tuple[str, ...],
    ) -> str:
        stdout = process.stdout
        if stdout is None:
            msg = "SteamCMD stdout pipe is unavailable"
            raise RuntimeError(msg)
        output: list[str] = []
        while line := await stdout.readline():
            value = redact(line.decode(errors="replace"), secrets)
            output.append(value)
            if self.log_handler is not None:
                self.log_handler(value.rstrip("\r\n"))
        await process.wait()
        return "".join(output)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        signal_process_group(process.pid, signal.SIGTERM)
        try:
            async with asyncio.timeout(TERMINATE_GRACE_PERIOD):
                await process.wait()
        except TimeoutError:
            signal_process_group(process.pid, signal.SIGKILL)
            await process.wait()
    signal_process_group(process.pid, signal.SIGKILL)


def signal_process_group(process_id: int, value: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_id, value)


def normalize_commands(
    commands: Iterable[SteamCMDCommand],
) -> tuple[tuple[str, ...], ...]:
    values: list[tuple[str, ...]] = []
    for command in commands:
        if isinstance(command, str):
            msg = "SteamCMD commands must be non-empty token sequences"
            raise TypeError(msg)
        tokens = tuple(command)
        if not tokens:
            msg = "SteamCMD commands must be non-empty token sequences"
            raise ValueError(msg)
        if not all(isinstance(token, str) for token in tokens):
            msg = "SteamCMD command tokens must be strings"
            raise TypeError(msg)
        name = tokens[0]
        if not COMMAND_NAME.fullmatch(name):
            msg = f"invalid SteamCMD command name: {name!r}"
            raise ValueError(msg)
        for token in tokens[1:]:
            validate_argument("SteamCMD argument", token)
        values.append(tokens)
    if not values:
        msg = "at least one SteamCMD command is required"
        raise ValueError(msg)
    return tuple(values)


def validate_argument(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    if not value.isprintable():
        msg = f"{name} must not contain control characters"
        raise ValueError(msg)
    return value


def quote_argument(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return value


def absolute_path(name: str, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        msg = f"{name} must be an absolute path"
        raise ValueError(msg)
    return path


def normalize_secrets(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    raw_values = {validate_argument("secret", value) for value in values}
    return tuple(
        sorted(
            raw_values | {quote_argument(value)[1:-1] for value in raw_values},
            key=len,
            reverse=True,
        )
    )


def redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "***")
    return value


__all__ = ["SteamCMD"]
