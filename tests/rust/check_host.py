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
import json, os, sys, time
unit, = (arg.split('=', 1)[1] for arg in sys.argv
         if arg.startswith(('--unit=', '--user-unit=')))
def emit(cursor, message, **fields):
    print(json.dumps({'__CURSOR': cursor, '__REALTIME_TIMESTAMP': '1000000',
                      'MESSAGE': message, **fields}), flush=True)
if f'--user-unit={unit}' in sys.argv:
    emit('agent', 'Agent stdout', _SYSTEMD_UNIT=f'user@{os.getuid()}.service',
         _SYSTEMD_USER_UNIT=unit, _UID=str(os.geteuid()))
else:
    # The Agent writes to user-1000.journal and PID 1 writes to system.journal.
    if '--system' not in sys.argv:
        emit('agent', 'Agent stdout', _SYSTEMD_UNIT=unit, _UID='1000')
    emit('manager', 'Started room', _SYSTEMD_UNIT='init.scope', UNIT=unit, _UID='0')
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


async def check_offline_failure(host: Host, root: Path) -> None:
    failure = {
        "attempt_id": "interrupted-attempt",
        "cause": "agent_interrupted",
        "trigger": {
            "kind": "agent_interrupted",
            "stage": "unknown",
            "code": None,
            "operation_id": None,
            "observed_at_ms": 100,
            "force_requested": False,
        },
        "shards": {},
        "complete": False,
        "classification": {"kind": "retryable"},
        "catalog_error": None,
        "decision": {
            "action": "stay_closed",
            "reason": "current_retries_exhausted",
        },
        "restarts_used": 2,
        "recovery_attempt_id": "last-retry",
        "result": "interrupted",
    }
    control_path = root / "001/.dst-server.json"
    control = json.loads(control_path.read_text())
    control["recovery"] = {
        "restarts_used": 2,
        "closed": "current_retries_exhausted",
        "last_failure": failure,
    }
    control_path.write_text(json.dumps(control))
    offline = await host.status(1)
    assert offline["game"] is None
    assert offline["recovery"]["last_failure"] == failure


async def check_journal(host: Host, *, user: bool) -> None:
    assert host.user == user
    logs = await host.journal([1], JournalQuery(limit=2))
    messages = [record.message for record in logs.records]
    assert messages == (["Agent stdout"] if user else ["Agent stdout", "Started room"])
    assert logs.records[0].emitting_unit(user=user) == "dst-001.service"
    if not user:
        assert logs.records[1].unit == "dst-001.service"
        assert logs.records[1].emitting_unit() == "init.scope"
    async with host.follow_journal([1]) as stream:
        record = await anext(stream)
        assert record.unit == "dst-001.service"
        assert record.message == "Agent stdout"


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

        assert await host.set_admin(1, "KU_test_user", enabled=True) is None
        blocklist = root / "001/blocklist.txt"
        native_ban = b"KU_banned\xba\xba1790989763\xba\xbaRoom \xff\xba\n"
        blocklist.write_bytes(native_ban)
        await host.edit_fields(1, [("/cluster/settings/cluster_name", "Updated name")])
        assert blocklist.read_bytes() == native_ban
        assert await host.blocklist(1) == ["KU_banned"]
        assert await host.is_blocked(1, "KU_banned")
        assert not await host.is_blocked(1, "KU_absent")
        assert await host.ban(1, "KU_second") is None
        assert await host.blocklist(1) == ["KU_banned", "KU_second"]
        assert blocklist.read_bytes() == native_ban + b"KU_second\n"
        assert await host.ban(1, "KU_banned") is None
        assert blocklist.read_bytes() == native_ban + b"KU_second\n"
        assert (await host.read_permissions(1))["adminlist"] == ["KU_test_user"]
        assert await host.is_admin(1, "KU_test_user")
        assert await host.set_admin(1, "KU_test_user", enabled=False) is None
        assert not await host.is_admin(1, "KU_test_user")
        assert await host.unban(1, "KU_banned") is None
        assert await host.unban(1, "KU_banned") is None
        assert not await host.is_blocked(1, "KU_banned")

        outcomes = await host.provision([definition(1), definition(2)])
        assert [entry["number"] for entry in outcomes] == [1, 2]
        assert [entry["result"]["ok"] for entry in outcomes] == [False, True]
        assert "host-fixture-token" not in json.dumps(outcomes)
        assert host.rooms.numbers() == [1, 2]

        statuses = await host.batch([2, 299, 1], {"operation": "status", "game": False})
        assert [entry["number"] for entry in statuses] == [2, 299, 1]
        assert [entry["result"]["ok"] for entry in statuses] == [True, False, True]
        statuses = await host.list()
        assert [entry["number"] for entry in statuses] == [1, 2]
        assert all(entry["result"]["ok"] for entry in statuses)
        assert statuses[0]["result"]["value"]["active"] == "inactive"
        host.rooms.save(definition(299))
        assert host.rooms.numbers() == [1, 2, 299]
        statuses = await host.list()
        assert [entry["number"] for entry in statuses] == [1, 2]
        assert (await host.status(1))["active"] == "inactive"
        await check_offline_failure(host, root)

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

        await check_journal(host, user=False)
        await check_journal(
            Host(root, units, journalctl=journalctl, user=True), user=True
        )
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
        command = [
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
        ]
        result = subprocess.run(
            [*command, "status", "--room", "1,299", "--service-only"],
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
        damaged = root / "002" / "cluster.ini"
        damaged.unlink()
        damaged.mkdir()
        (units / "dst-298.container").write_text("[Container]\nImage=fixture\n")
        result = subprocess.run(
            [*command, "list"], capture_output=True, text=True, check=False, timeout=30
        )
        assert result.returncode == 0, result.stderr
        listed = json.loads(result.stdout)
        assert [entry["number"] for entry in listed] == [1, 2]
        assert listed[1]["result"]["value"]["configuration_error"]
        assert (
            listed[0]["result"]["value"]["recovery"]["last_failure"]["cause"]
            == "agent_interrupted"
        )
        result = subprocess.run(
            [*command, "stop", "--all", "--no-wait"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert [entry["number"] for entry in json.loads(result.stdout)] == [1, 2]
        trace = systemctl.with_suffix(".trace").read_bytes()
        command[command.index(str(root))] = str(root.with_name("other-rooms"))
        result = subprocess.run(
            [*command, "stop", "--all", "--no-wait"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 1
        assert systemctl.with_suffix(".trace").read_bytes() == trace
        command[command.index(str(units))] = str(units.with_name("other-units"))
        result = subprocess.run(
            [*command, "stop", "--room", "1", "--no-wait"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 1
        assert "no managed Quadlet" in result.stdout
        assert systemctl.with_suffix(".trace").read_bytes() == trace


if __name__ == "__main__":
    main()
