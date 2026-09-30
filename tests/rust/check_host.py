"""Exercise the installed native Host with disposable rooms and service fixtures."""

# All child programs and credentials below are disposable test fixtures.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, hardcoded-password-func-arg, hardcoded-password-string, pytest-assert-in-except]

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from dst_server import DstError
from dst_server.host import Host
from dst_server.logs import JournalQuery
from dst_server.settings import Room, build_template


def executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


SYSTEMCTL = """
import pathlib, sys
path = pathlib.Path(sys.argv[0])
with path.with_suffix('.trace').open('a') as trace:
    trace.write(' '.join(sys.argv[1:]) + '\\n')
if 'show' in sys.argv:
    unit = sys.argv[-1]
    if unit == 'dst-299.service':
        print('fixture service failure', file=sys.stderr)
        sys.exit(1)
    print(f'Id={unit}\\nLoadState=loaded\\nActiveState=inactive\\n'
          'SubState=dead\\nJob=0\\nResult=success')
"""

JOURNAL = """
import json, sys, time
print(json.dumps({
    '__CURSOR': 'host-log-1', '__REALTIME_TIMESTAMP': '1000000',
    '_SYSTEMD_UNIT': 'dst-001.service', 'MESSAGE': 'host fixture log',
}), flush=True)
if '--follow' in sys.argv:
    time.sleep(60)
"""


def definition(number: int) -> Room:
    return Room({
        "number": number,
        "template": "pure_survival",
        "cluster": build_template(
            "pure_survival",
            number=number,
            token="host-fixture-token",
            cluster_key="host-fixture-key",
        ).dump(secrets=True),
    })


async def check(directory: Path) -> tuple[Path, Path, Path]:  # ruff: ignore[too-many-statements]
    root, units = directory / "rooms", directory / "units"
    systemctl = executable(directory, "systemctl", SYSTEMCTL)
    journalctl = executable(directory, "journalctl", JOURNAL)
    async with Host(root, units, systemctl=systemctl, journalctl=journalctl) as host:
        created = await host.create(definition(1))
        assert created.number == 1
        assert host.rooms.numbers() == [1]
        assert host.units(1) == ["dst-001.service"]
        assert "host-fixture-token" not in repr(created)
        assert created.dump(secrets=True)["cluster"]["token"] == "host-fixture-token"

        updated = await host.edit_fields(
            1, [("/cluster/settings/cluster_name", "Native host room")]
        )
        assert updated.get("/cluster/settings/cluster_name") == "Native host room"
        assert (await host.load(1)).get("/cluster/token") != "host-fixture-token"
        assert (await host.load(1)).dump(secrets=True)["cluster"]["token"] == (
            "host-fixture-token"
        )

        before = (root / "001/cluster.ini").read_bytes()
        try:
            await host.edit_fields(1, [("/cluster/settings/max_players", 0)])
        except ValueError:
            pass
        else:
            message = "invalid configuration was accepted"
            raise AssertionError(message)
        assert (root / "001/cluster.ini").read_bytes() == before

        assert await host.permission(1, "admin", "KU_test_user") == ["KU_test_user"]
        blocklist = root / "001/blocklist.txt"
        native_ban = b"KU_banned\xba\xba1790989763\xba\xbaRoom \xff\xba\n"
        blocklist.write_bytes(native_ban)
        await host.edit_fields(1, [("/cluster/settings/cluster_name", "Updated name")])
        assert blocklist.read_bytes() == native_ban
        assert await host.permission(1, "ban") == ["KU_banned"]
        assert await host.permission(1, "ban", "KU_second") == [
            "KU_banned",
            "KU_second",
        ]
        assert blocklist.read_bytes() == native_ban + b"KU_second\n"
        assert await host.permission(1, "admin") == ["KU_test_user"]
        assert await host.permission(1, "admin", "KU_test_user", remove=True) == []

        outcomes = await host.provision([definition(1), definition(2)])
        assert [entry["number"] for entry in outcomes] == [1, 2]
        assert [entry["result"]["ok"] for entry in outcomes] == [False, True]
        assert "host-fixture-token" not in json.dumps(outcomes)
        assert host.rooms.numbers() == [1, 2]

        statuses = await host.batch([2, 299, 1], {"operation": "status", "game": False})
        assert [entry["number"] for entry in statuses] == [2, 299, 1]
        assert [entry["result"]["ok"] for entry in statuses] == [True, False, True]
        assert len(await host.list()) == 2
        assert (await host.status(1))["active"] == "inactive"

        trace = systemctl.with_suffix(".trace")
        before = trace.read_bytes()
        for numbers in ([1, 1], [300], [True]):
            try:
                await host.batch(numbers, {"operation": "stop", "wait": False})
            except ValueError:
                pass
            else:
                message = "invalid batch was accepted"
                raise AssertionError(message)
        assert trace.read_bytes() == before

        await host.start(1, wait=False)
        await host.restart(1, wait=False)
        assert (await host.stop(1))["active"] == "inactive"
        commands = trace.read_text(encoding="utf-8")
        for action in ("start", "restart", "stop"):
            assert f"--no-block {action} -- dst-001.service" in commands

        logs = await host.journal([1], JournalQuery(limit=1))
        assert logs.records[0].message == "host fixture log"
        async with host.follow_journal([1]) as stream:
            assert (await anext(stream)).unit == "dst-001.service"
        assert (await host.diagnose(1))["logs"]["records"]

        try:
            await host.call(1, "status")
        except DstError as error:
            assert error.code
        else:
            message = "missing Agent connection succeeded"
            raise AssertionError(message)

    return root, units, systemctl


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root, units, systemctl = asyncio.run(check(Path(temporary)))
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "dst_server",
                "--json",
                "host",
                "--root",
                str(root),
                "--quadlet-dir",
                str(units),
                "--systemctl",
                str(systemctl),
                "status",
                "--room",
                "1,299",
                "--service-only",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 1, result.stderr
        assert [entry["result"]["ok"] for entry in json.loads(result.stdout)] == [
            True,
            False,
        ]


if __name__ == "__main__":
    main()
