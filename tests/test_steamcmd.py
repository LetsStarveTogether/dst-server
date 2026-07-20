from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from dst_server.steamcmd import SteamCMD

FAKE_STEAMCMD = r"""#!/usr/bin/env python3
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

assert sys.argv[1] == "+runscript"
script = Path(sys.argv[2])
content = script.read_text(encoding="utf-8")
print("ARGV|" + "|".join(sys.argv[1:]), flush=True)
print("HOME|" + os.environ.get("HOME", ""), flush=True)
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
    if tokens[0] == "serialize":
        marker = Path(tokens[1])
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            time.sleep(0.1)
        finally:
            os.close(descriptor)
            marker.unlink()
    if tokens[0] == "hang":
        marker = Path(tokens[1])
        child = subprocess.Popen([
            sys.executable,
            "-c",
            "import os,signal,sys,time;"
            "from pathlib import Path;"
            "Path(sys.argv[1]).write_text(str(os.getpid()),encoding='utf-8');"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "time.sleep(60)",
            str(marker),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not marker.exists():
            time.sleep(0.01)
        child.wait()
"""


def make_client(
    tmp_path: Path,
    *,
    log_handler: list[str] | None = None,
) -> SteamCMD:
    executable = tmp_path / "fake-steamcmd"
    executable.write_text(FAKE_STEAMCMD, encoding="utf-8")
    executable.chmod(0o755)
    return SteamCMD(
        executable,
        steam_home=tmp_path / "steam home",
        log_handler=None if log_handler is None else log_handler.append,
    )


def script_from(output: str) -> str:
    return output.split("SCRIPT-BEGIN\n", 1)[1].split("SCRIPT-END\n", 1)[0]


def path_missing(path: Path) -> bool:
    return not path.exists()


def process_stopped(process_id: int) -> bool:
    for _ in range(100):
        status = Path(f"/proc/{process_id}/stat")
        try:
            state = status.read_text(encoding="utf-8").split()[2]
        except FileNotFoundError, ProcessLookupError:
            return True
        if state == "Z":
            return True
        time.sleep(0.01)
    return False


def wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 2
    while not path.exists():
        if time.monotonic() >= deadline:
            msg = f"timed out waiting for {path}"
            raise TimeoutError(msg)
        time.sleep(0.01)


async def test_execute_uses_isolated_home_script_and_streaming_log(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    client = make_client(tmp_path, log_handler=lines)

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
    assert path_missing(script_path)
    assert "SCRIPT-BEGIN" in lines


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
    client = make_client(tmp_path, log_handler=lines)

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
    client = make_client(tmp_path)
    marker = tmp_path / "timeout-child.pid"

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await client.execute([("hang", str(marker))])

    process_id = int(marker.read_text(encoding="utf-8"))
    assert await asyncio.to_thread(process_stopped, process_id)


async def test_cancellation_terminates_process_group(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    marker = tmp_path / "cancelled-child.pid"
    task = asyncio.create_task(client.execute([("hang", str(marker))]))
    await asyncio.to_thread(wait_for_path, marker)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    process_id = int(marker.read_text(encoding="utf-8"))
    assert await asyncio.to_thread(process_stopped, process_id)


async def test_client_serializes_shared_steam_home(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    marker = tmp_path / "active"

    await asyncio.gather(
        client.execute([("serialize", str(marker))]),
        client.execute([("serialize", str(marker))]),
    )

    assert path_missing(marker)


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
async def test_real_steamcmd_app_and_depot_query() -> None:
    executable = os.environ.get("DST_SERVER_STEAMCMD", "/usr/bin/steamcmd")
    with TemporaryDirectory(prefix="dst-server-steamcmd-test-", dir="/tmp") as value:
        root = Path(value)
        client = SteamCMD(executable, steam_home=root / "home")
        async with asyncio.timeout(180):
            info = await client.app_info(343050)
            depots = await client.depot_info(343050)
        assert '"343050"' in info
        assert '"depots"' in depots
    assert path_missing(root)
