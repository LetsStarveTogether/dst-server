import asyncio
import os
import signal
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dst_server.mods import SteamCMD, steamcmd
from dst_server.mods import process as mod_process
from tests.helpers import BlockingProcess, process_stopped

FAKE_STEAMCMD = r"""#!/usr/bin/env python3
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

assert sys.argv[1] == "+runscript"
script = Path(sys.argv[2])
content = script.read_text(encoding="utf-8")
print("ARGV|" + "|".join(sys.argv[1:]), flush=True)
print("HOME|" + os.environ.get("HOME", ""), flush=True)
print("PROXY|" + "|".join(os.environ.get(name, "") for name in (
    "http_proxy", "https_proxy"
)), flush=True)
print("OTHER_PROXY|" + "|".join(os.environ.get(name, "") for name in (
    "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
    "ftp_proxy", "FTP_PROXY", "no_proxy", "NO_PROXY"
)), flush=True)
print(f"MODE|{script.stat().st_mode & 0o777:o}", flush=True)
print("SCRIPT-BEGIN", flush=True)
print(content, end="", flush=True)
print("SCRIPT-END", flush=True)

for line in content.splitlines():
    tokens = shlex.split(line)
    if not tokens:
        continue
    if tokens[0] == "fail":
        print("failure: " + tokens[1], flush=True)
        raise SystemExit(7)
    if tokens[0] in {"hang", "stubborn"}:
        if tokens[0] == "stubborn":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([
            sys.executable,
            "-c",
            "import signal;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "print('READY', flush=True);"
            "signal.pause()",
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert child.stdout.readline() == b"READY\n"
        print(f"CHILD|{child.pid}", flush=True)
        child.wait()
    if tokens[0] == "orphan":
        child = subprocess.Popen([
            sys.executable,
            "-c",
            "import signal;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "signal.pause()",
        ])
        print(f"CHILD|{child.pid}", flush=True)
        raise SystemExit(0)
"""


def make_client(
    tmp_path: Path,
    *,
    log_handler: Callable[[str], None] | None = None,
    proxy: str | None = None,
) -> SteamCMD:
    executable = tmp_path / "fake-steamcmd"
    executable.write_text(FAKE_STEAMCMD, encoding="utf-8")
    executable.chmod(0o755)
    return SteamCMD(
        executable,
        steam_home=tmp_path / "steam home",
        log_handler=log_handler,
        proxy=proxy,
    )


def script_from(output: str) -> str:
    return output.split("SCRIPT-BEGIN\n", 1)[1].split("SCRIPT-END\n", 1)[0]


async def test_execute_uses_isolated_home_script_and_streaming_log(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    client = make_client(tmp_path, log_handler=lines.append)

    output = await client.run([
        ("login", "anonymous"),
        ("custom_command", "hello world"),
    ])

    script = script_from(output)
    script_path = Path(output.splitlines()[0].split("|", 2)[2])
    assert f"HOME|{tmp_path / 'steam home'}" in output
    assert "MODE|600" in output
    assert script.startswith("@ShutdownOnFailedCommand 1\n@NoPromptForPassword 1\n")
    assert 'custom_command "hello world"\n' in script
    assert script.endswith("quit\n")
    assert not await asyncio.to_thread(script_path.exists)
    assert "SCRIPT-BEGIN" in lines


@pytest.mark.parametrize("proxy", [None, "http://user:secret@127.0.0.1:1080"])
async def test_explicit_proxy_is_scoped_to_steamcmd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proxy: str | None,
) -> None:
    proxy_variables = (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
    )
    for name in proxy_variables:
        monkeypatch.setenv(name, "http://inherited.invalid")
    output = await make_client(tmp_path, proxy=proxy).run([("noop",)])

    assert ("PROXY|***|***" if proxy else "PROXY||\n") in output
    assert "inherited.invalid" not in output
    assert "secret" not in output
    assert all(
        os.environ[name] == "http://inherited.invalid" for name in proxy_variables
    )


@pytest.mark.parametrize(
    "proxy",
    [
        "",
        "socks5://localhost:1080",
        "http://",
        "http://secret@host:bad",
        "http://host\n",
    ],
)
def test_download_proxy_rejects_invalid_urls_without_echoing_credentials(
    proxy: str,
) -> None:
    with pytest.raises(ValueError, match="download proxy") as error:
        SteamCMD("steamcmd", proxy=proxy)
    assert "secret" not in str(error.value)


async def test_failure_redacts_output_and_removes_script(tmp_path: Path) -> None:
    lines: list[str] = []
    client = make_client(tmp_path, log_handler=lines.append)

    with pytest.raises(ChildProcessError, match="status 7") as error:
        await client.run([("fail", "secret-value")], secrets=("secret-value",))

    assert "secret-value" not in str(error.value)
    assert all("secret-value" not in line for line in lines)
    assert not tuple((tmp_path / "steam home").glob(".dst-server-steamcmd-*"))


async def test_spawn_failure_removes_script(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client = SteamCMD(tmp_path / "missing-steamcmd", steam_home=home)

    with pytest.raises(FileNotFoundError):
        await client.run([("command",)])

    assert not tuple(home.glob(".dst-server-steamcmd-*"))


async def test_external_timeout_terminates_process_group(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    child: asyncio.Future[int] = loop.create_future()
    timeout = asyncio.timeout(2)

    def handle_log(line: str) -> None:
        if line.startswith("CHILD|") and not child.done():
            child.set_result(int(line.partition("|")[2]))
            timeout.reschedule(loop.time())

    client = make_client(tmp_path, log_handler=handle_log)

    with pytest.raises(TimeoutError):
        async with timeout:
            await client.run([("hang",)])

    process_id = child.result()
    assert await asyncio.to_thread(process_stopped, process_id)


async def test_cancellation_terminates_process_group(tmp_path: Path) -> None:
    started = asyncio.Event()
    process_id = 0

    def handle_log(line: str) -> None:
        nonlocal process_id
        if line.startswith("CHILD|"):
            process_id = int(line.partition("|")[2])
            started.set()

    client = make_client(tmp_path, log_handler=handle_log)
    task = asyncio.create_task(client.run([("hang",)]))
    async with asyncio.timeout(2):
        await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(2):
            await task

    assert await asyncio.to_thread(process_stopped, process_id)


async def test_leader_exit_terminates_descendant_holding_stdout(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)

    async with asyncio.timeout(2):
        output = await client.run([("orphan",)])

    process_id = int(
        next(
            line for line in output.splitlines() if line.startswith("CHILD|")
        ).partition("|")[2]
    )
    assert await asyncio.to_thread(process_stopped, process_id)


async def test_repeated_cancellation_still_kills_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    terminating = asyncio.Event()
    child = 0
    signal_group = mod_process.signal_process_group

    def signal_process_group(process_id: int, value: signal.Signals) -> None:
        signal_group(process_id, value)
        terminating.set()

    def handle_log(line: str) -> None:
        nonlocal child
        if line.startswith("CHILD|"):
            child = int(line.partition("|")[2])
            started.set()

    monkeypatch.setattr(mod_process, "TERMINATE_GRACE_PERIOD", 0.05)
    monkeypatch.setattr(mod_process, "signal_process_group", signal_process_group)
    task = asyncio.create_task(
        make_client(tmp_path, log_handler=handle_log).run([
            ("stubborn",),
        ])
    )
    try:
        async with asyncio.timeout(2):
            await started.wait()
            task.cancel()
            await terminating.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await asyncio.to_thread(process_stopped, child)
    finally:
        # A failing regression must not leave its deliberately stubborn child alive.
        if child:
            with suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)


@pytest.mark.skipif(
    os.environ.get("DST_SERVER_STEAMCMD_TEST") != "1",
    reason="set DST_SERVER_STEAMCMD_TEST=1 to run the real SteamCMD query test",
)
@pytest.mark.system
async def test_real_steamcmd_depot_query(tmp_path: Path) -> None:
    executable = os.environ.get("DST_SERVER_STEAMCMD", "/usr/bin/steamcmd")
    home = tmp_path / "home"
    client = SteamCMD(executable, steam_home=home)
    async with asyncio.timeout(180):
        depots = await client.run([
            ("app_info_update", "1"),
            ("app_info_print", "343050", "depots"),
        ])
    assert '"depots"' in depots
    assert not tuple(home.glob(".dst-server-steamcmd-*"))


@pytest.mark.parametrize("argument", ["hello world", 'quote"and\\slash', "游戏🙂"])
async def test_run_quotes_arguments_and_redacts_raw_and_escaped_secrets(
    tmp_path: Path, argument: str
) -> None:
    client = make_client(tmp_path)
    output = await client.run([("custom_command", argument)], install_dir=tmp_path)
    script = script_from(output)
    assert (
        script.index("force_install_dir")
        < script.index("login")
        < script.index("custom_command")
    )
    assert f"custom_command {steamcmd.quote_argument(argument)}\n" in script
    hidden = await client.run([("custom_command", argument)], secrets=(argument,))
    assert argument not in hidden
    assert steamcmd.quote_argument(argument)[1:-1] not in hidden


@pytest.mark.parametrize(
    ("commands", "error"),
    [
        ([], ValueError),
        ([()], ValueError),
        (["bad"], TypeError),
        ([("bad command",)], ValueError),
        ([("command", "bad\nargument")], ValueError),
        ([("command", 42)], TypeError),
    ],
)
def test_commands_reject_invalid_tokens(commands: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        steamcmd.normalize_commands(commands)


async def test_run_rejects_relative_install_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        await make_client(tmp_path).run([("noop",)], install_dir="relative")


async def test_run_chains_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = BlockingProcess()

    def fail_log(_line: str) -> None:
        msg = "primary A"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=blocked)
    )
    monkeypatch.setattr(
        mod_process,
        "terminate_process",
        AsyncMock(side_effect=LookupError("cleanup B")),
    )
    existing_tasks = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="primary A") as caught:
        await make_client(tmp_path, log_handler=fail_log).run([("noop",)])
    assert isinstance(caught.value.__cause__, LookupError)
    assert str(caught.value.__cause__) == "cleanup B"
    assert asyncio.all_tasks() == existing_tasks
    assert not tuple((tmp_path / "steam home").glob(".dst-server-steamcmd-*"))


async def test_client_serializes_executions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    second_normalized = asyncio.Event()
    calls = 0

    async def run_process(*_command: str, **_options: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return 0

    def commands() -> Iterable[tuple[str, ...]]:
        second_normalized.set()
        yield ("noop",)

    monkeypatch.setattr(steamcmd, "run_process", run_process)
    first = asyncio.create_task(client.run([("noop",)]))
    await entered.wait()
    second = asyncio.create_task(client.run(commands()))
    try:
        async with asyncio.timeout(1):
            await second_normalized.wait()
        assert calls == 1
    finally:
        release.set()
        async with asyncio.timeout(1):
            await asyncio.gather(first, second)
    assert calls == 2
