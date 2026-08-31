import asyncio
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

import pytest

from dst_server import steamcmd
from dst_server.steamcmd import SteamCMD

from .helpers import BlockingProcess, process_stopped

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
    if tokens[0] == "hang":
        child = subprocess.Popen([
            sys.executable,
            "-c",
            "import signal;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "signal.pause()",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
) -> SteamCMD:
    executable = tmp_path / "fake-steamcmd"
    executable.write_text(FAKE_STEAMCMD, encoding="utf-8")
    executable.chmod(0o755)
    return SteamCMD(
        executable,
        steam_home=tmp_path / "steam home",
        log_handler=log_handler,
    )


def script_from(output: str) -> str:
    return output.split("SCRIPT-BEGIN\n", 1)[1].split("SCRIPT-END\n", 1)[0]


async def test_execute_uses_isolated_home_script_and_streaming_log(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    client = make_client(tmp_path, log_handler=lines.append)

    output = await client.execute([
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


async def test_dedicated_proxy_is_scoped_to_steamcmd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_variables = ("http_proxy", "https_proxy")
    for name in proxy_variables:
        monkeypatch.setenv(name, "http://inherited.invalid")
    monkeypatch.setenv(
        "DST_SERVER_STEAMCMD_PROXY",
        "http://user:secret@127.0.0.1:1080",
    )

    output = await make_client(tmp_path).execute([("noop",)])

    assert "PROXY|***|***" in output
    assert "secret" not in output
    assert all(
        os.environ[name] == "http://inherited.invalid" for name in proxy_variables
    )


async def test_query_commands(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    install = tmp_path / "install"

    assert 'app_info_update "1"' in await client.app_info(343050)
    assert 'app_info_print "343050" "depots"' in await client.depot_info(343050)
    assert 'app_status "343050"' in await client.app_status(
        343050,
        install_dir=install,
    )
    assert 'app_update_status "343050"' in await client.app_update_status(
        343050,
        install_dir=install,
    )
    assert 'app_installed_files "343050"' in await client.app_installed_files(
        343050,
        install_dir=install,
    )
    assert "apps_installed\n" in await client.apps_installed()
    assert 'package_info_print "17906"' in await client.package_info(17906)
    assert 'app_dlc_status "343050" "343051"' in await client.dlc_status(
        343050,
        343051,
    )
    assert 'workshop_status "322330"' in await client.workshop_status(322330)


async def test_update_validate_and_maintenance_commands(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    install = tmp_path / "install"
    destination = tmp_path / "depot"
    backup = tmp_path / "backup"
    redaction_value = f'private\\"{tmp_path.name}'

    output = await client.update(
        343050,
        install,
        beta="updatebeta",
        beta_password=redaction_value,
        language="english",
        platform="linux",
    )
    script = script_from(output)
    assert redaction_value not in output
    assert redaction_value.replace("\\", "\\\\").replace('"', '\\"') not in output
    assert '@sSteamCmdForcePlatformType "linux"' in script
    assert f'force_install_dir "{install}"' in script
    assert (
        script.index("@sSteamCmdForcePlatformType")
        < script.index("force_install_dir")
        < script.index("login")
        < script.index("app_update")
    )
    assert (
        'app_update "343050" "-beta" "updatebeta" '
        '"-betapassword" "***" "-language" "english"'
    ) in script
    update_command = next(
        line for line in script.splitlines() if line.startswith("app_update ")
    )
    assert '"validate"' not in update_command

    assert 'app_update "343050" "validate"' in await client.validate(
        343050,
        install,
    )
    assert (
        f'download_depot "343050" "343051" "10" "9" "{destination}"'
        in await client.download_depot(
            343050,
            343051,
            manifest_id=10,
            delta_manifest_id=9,
            destination=destination,
        )
    )
    assert (
        'workshop_download_item "322330" "123456"'
        in await client.workshop_download_item(322330, 123456)
    )
    assert 'app_uninstall "343050"' in await client.uninstall(343050, install)
    assert f'app_backup "343050" "{backup}" "512"' in await client.backup_app(
        343050, backup, max_folder_size_mb=512
    )
    assert 'app_license_request "343050"' in await client.request_license(343050)


async def test_failure_redacts_output_and_removes_script(tmp_path: Path) -> None:
    lines: list[str] = []
    client = make_client(tmp_path, log_handler=lines.append)

    with pytest.raises(ChildProcessError, match="status 7") as error:
        await client.execute([("fail", "secret-value")], secrets=("secret-value",))

    assert "secret-value" not in str(error.value)
    assert all("secret-value" not in line for line in lines)
    assert not tuple((tmp_path / "steam home").glob(".dst-server-steamcmd-*"))


async def test_spawn_failure_removes_script(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client = SteamCMD(tmp_path / "missing-steamcmd", steam_home=home)

    with pytest.raises(FileNotFoundError):
        await client.execute([("command",)])

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
            await client.execute([("hang",)])

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
    task = asyncio.create_task(client.execute([("hang",)]))
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
        output = await client.execute([("orphan",)])

    process_id = int(
        next(
            line for line in output.splitlines() if line.startswith("CHILD|")
        ).partition("|")[2]
    )
    assert await asyncio.to_thread(process_stopped, process_id)


@pytest.mark.parametrize(
    ("cancelled", "interrupt_cleanup"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_cleanup_process_tasks_preserves_primary_and_reaps_tasks(
    cancelled: bool,
    interrupt_cleanup: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = asyncio.Event()
    primary: BaseException = (
        asyncio.CancelledError("primary A") if cancelled else RuntimeError("primary A")
    )

    async def blocked() -> object:
        await blocker.wait()
        return None

    async def fail_or_block() -> object:
        if cancelled:
            await blocker.wait()
        raise primary

    tasks: tuple[asyncio.Task[object], ...] = (
        asyncio.create_task(blocked()),
        asyncio.create_task(fail_or_block()),
    )
    await asyncio.sleep(0)

    async def fail_cleanup(_process: object) -> None:  # ruff:ignore[unused-async]
        msg = "cleanup B"
        raise LookupError(msg)

    monkeypatch.setattr(steamcmd, "terminate_process", fail_cleanup)
    if interrupt_cleanup:
        current = asyncio.current_task()
        assert current is not None
        asyncio.get_running_loop().call_soon(current.cancel, "cleanup cancel")

    cleanup_error = await steamcmd.cleanup_process_tasks(
        cast(asyncio.subprocess.Process, BlockingProcess()),
        primary,
        *tasks,
    )

    if interrupt_cleanup:
        assert isinstance(cleanup_error, BaseExceptionGroup)
        assert [type(error) for error in cleanup_error.exceptions] == [
            LookupError,
            asyncio.CancelledError,
        ]
    else:
        assert isinstance(cleanup_error, LookupError)
        assert str(cleanup_error) == "cleanup B"
    assert all(task.done() for task in tasks)


async def test_execute_script_chains_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = BlockingProcess()

    async def spawn(  # ruff:ignore[unused-async]
        *_args: object,
        **_kwargs: object,
    ) -> BlockingProcess:
        return process

    async def fail_cleanup(_process: object) -> None:  # ruff:ignore[unused-async]
        msg = "cleanup B"
        raise LookupError(msg)

    def fail_log(_line: str) -> None:
        msg = "primary A"
        raise RuntimeError(msg)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(steamcmd, "terminate_process", fail_cleanup)
    client = SteamCMD(tmp_path / "steamcmd", log_handler=fail_log)
    existing_tasks = asyncio.all_tasks()

    with pytest.raises(RuntimeError, match="primary A") as caught:
        await client.execute_script(tmp_path / "script", ())

    assert isinstance(caught.value.__cause__, LookupError)
    assert str(caught.value.__cause__) == "cleanup B"
    assert asyncio.all_tasks() == existing_tasks


async def test_client_serializes_executions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(tmp_path)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_normalized = asyncio.Event()
    calls = 0

    async def execute_script(script: Path, secrets: tuple[str, ...]) -> str:
        nonlocal calls
        del script, secrets
        calls += 1
        if calls == 1:
            first_entered.set()
            await release_first.wait()
        return ""

    def second_commands() -> Iterable[tuple[str, ...]]:
        second_normalized.set()
        yield ("noop",)

    monkeypatch.setattr(client, "execute_script", execute_script)
    first = asyncio.create_task(client.execute([("noop",)]))
    async with asyncio.timeout(1):
        await first_entered.wait()
    second = asyncio.create_task(client.execute(second_commands()))

    try:
        async with asyncio.timeout(1):
            await second_normalized.wait()
        assert calls == 1
    finally:
        release_first.set()
        async with asyncio.timeout(1):
            await asyncio.gather(first, second)

    assert calls == 2


async def test_input_validation(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    redaction_value = f"secret-{tmp_path.name}"

    with pytest.raises(ValueError, match="positive integer"):
        await client.app_info(0)
    with pytest.raises(ValueError, match="absolute path"):
        await client.update(343050, Path("relative"))
    with pytest.raises(ValueError, match="requires a beta branch"):
        await client.update(343050, tmp_path, beta_password=redaction_value)
    with pytest.raises(ValueError, match="platform"):
        await client.update(343050, tmp_path, platform="plan9")
    with pytest.raises(ValueError, match="target manifest"):
        await client.download_depot(1, 2, delta_manifest_id=3)
    with pytest.raises(ValueError, match="requires both manifest IDs"):
        await client.download_depot(1, 2, manifest_id=3, destination=tmp_path)
    with pytest.raises(ValueError, match="command name"):
        await client.execute([("bad command",)])
    with pytest.raises(ValueError, match="control characters"):
        await client.execute([("command", "bad\nargument")])
    with pytest.raises(ValueError, match="non-empty token"):
        await client.execute([()])


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
        depots = await client.depot_info(343050)
    assert '"depots"' in depots
    assert not tuple(home.glob(".dst-server-steamcmd-*"))
