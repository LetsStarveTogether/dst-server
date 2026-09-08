import asyncio
import os
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile

from .process import (
    download_environment,
    redact,
    run_process,
    validate_argument,
    validate_proxy,
)

COMMAND_NAME = re.compile(r"[A-Za-z0-9_@.-]+\Z")


class SteamCMD:
    """Run tokenized SteamCMD commands with an explicit download environment."""

    def __init__(
        self,
        executable: str | Path,
        *,
        steam_home: str | Path | None = None,
        username: str = "anonymous",
        proxy: str | None = None,
        log_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.executable = os.fspath(executable)
        if not self.executable:
            msg = "SteamCMD executable must not be empty"
            raise ValueError(msg)
        self.steam_home = (
            None if steam_home is None else absolute_path("SteamCMD home", steam_home)
        )
        self.username = validate_argument("username", username)
        self.proxy = validate_proxy(proxy)
        self.log_handler = log_handler
        self._lock = asyncio.Lock()

    async def run(
        self,
        commands: Iterable[Sequence[str]],
        *,
        install_dir: str | Path | None = None,
        secrets: Collection[str] = (),
    ) -> str:
        selected = normalize_commands(commands)
        secret_values = normalize_secrets(secrets)
        if self.proxy is not None:
            secret_values = normalize_secrets((*secret_values, self.proxy))
        prefix: tuple[tuple[str, ...], ...] = ()
        if install_dir is not None:
            prefix = (
                (
                    "force_install_dir",
                    str(absolute_path("install directory", install_dir)),
                ),
            )
        commands = (*prefix, ("login", self.username), *selected)
        environment = download_environment(self.proxy)
        if self.steam_home is not None:
            environment["HOME"] = str(self.steam_home)
        output: list[str] = []

        def on_line(line: str) -> None:
            value = redact(line, secret_values)
            output.append(value)
            if self.log_handler is not None:
                self.log_handler(value.rstrip("\r\n"))

        async with self._lock:
            if self.steam_home is not None:
                self.steam_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".dst-server-steamcmd-",
                suffix=".txt",
                dir=self.steam_home,
                delete_on_close=False,
            ) as script:
                script.write("@ShutdownOnFailedCommand 1\n@NoPromptForPassword 1\n")
                for command in commands:
                    script.write(command[0])
                    for argument in command[1:]:
                        script.write(f" {quote_argument(argument)}")
                    script.write("\n")
                script.write("quit\n")
                script.flush()
                returncode = await run_process(
                    self.executable,
                    "+runscript",
                    script.name,
                    cwd=self.steam_home,
                    environment=environment,
                    on_line=on_line,
                )
        result = "".join(output)
        if returncode:
            detail = result[-4000:].strip()
            suffix = "" if not detail else f": {detail}"
            msg = f"SteamCMD exited with status {returncode}{suffix}"
            raise ChildProcessError(msg)
        return result


def normalize_commands(
    commands: Iterable[Sequence[str]],
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
        if not COMMAND_NAME.fullmatch(tokens[0]):
            msg = f"invalid SteamCMD command name: {tokens[0]!r}"
            raise ValueError(msg)
        for token in tokens[1:]:
            validate_argument("SteamCMD argument", token)
        values.append(tokens)
    if not values:
        msg = "at least one SteamCMD command is required"
        raise ValueError(msg)
    return tuple(values)


def absolute_path(name: str, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        msg = f"{name} must be an absolute path"
        raise ValueError(msg)
    return path


def quote_argument(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
