import asyncio
import json
import os
import signal
import sys
import textwrap
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from pydantic import SecretStr

from dst_server.host import maintenance
from dst_server.host.service import Host
from dst_server.presets.lst import fleet_room
from dst_server.rooms import (
    CONTROL_FILE,
    RoomStore,
    control_revision,
    read_control,
    write_control,
)
from tests.helpers import wait_for_event


def host_at(tmp_path: Path) -> Mock:
    host = Mock(spec=Host)
    host.cluster_root = tmp_path / "cluster"
    host.quadlet_dir = tmp_path / "quadlet"
    host.rooms = RoomStore(host.cluster_root)
    for number in (1, 2):
        host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
    host.restart = AsyncMock()
    host.announce = AsyncMock()
    host.systemd.start_transient = AsyncMock()
    host.systemd.properties = AsyncMock()
    host.systemd.stop = AsyncMock()
    return host


@pytest.fixture
async def countdown_clock(  # ruff: ignore[unused-async]
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    elapsed = 1000.0
    delays: list[float] = []
    loop = Mock(wraps=asyncio.get_running_loop())
    loop.time.side_effect = lambda: elapsed

    async def sleep(delay: float) -> None:
        nonlocal elapsed
        delays.append(delay)
        elapsed += delay
        await asyncio.sleep(0)

    api = SimpleNamespace(**vars(asyncio))
    api.get_running_loop = lambda: loop
    api.sleep = sleep
    monkeypatch.setattr(maintenance, "asyncio", api)
    return delays


async def test_countdown_announces_each_minute_before_restart(
    tmp_path: Path, countdown_clock: list[float]
) -> None:
    host = host_at(tmp_path)
    revision = control_revision(host.rooms.path(1))
    messages: list[str] = []
    host.announce.side_effect = lambda _number, message: messages.append(message)

    async def restart(*_: object, **__: object) -> None:  # ruff: ignore[unused-async]
        assert messages == [
            # Exact player-facing announcement is part of the maintenance contract.
            f"本房间将在约 {minutes} 分钟后维护重启，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
            for minutes in range(8, 0, -1)
        ]
        assert countdown_clock == [60] * 8

    host.restart.side_effect = restart
    async with asyncio.timeout(5):
        assert await maintenance.maintain_restart(host, [1]) == {
            1: {"status": "restarted"}
        }
    host.restart.assert_awaited_once_with(
        1, override=False, expected_revision=revision, timeout=10800
    )


async def test_restart_isolates_room_failures(tmp_path: Path) -> None:
    host = host_at(tmp_path)
    host.restart.side_effect = [RuntimeError("game failed"), None]
    result = await maintenance.maintain_restart(host, [1, 2], delay=0)
    assert result == {
        1: {"status": "failed", "error": "game failed"},
        2: {"status": "restarted"},
    }


async def test_new_manual_operation_invalidates_pending_restart(
    tmp_path: Path, countdown_clock: list[float]
) -> None:
    host = host_at(tmp_path)
    started = asyncio.Event()

    def announce(*_: object) -> None:
        started.set()
        control = read_control(host.rooms.path(1))
        write_control(
            host.rooms.path(1),
            control.model_copy(update={"revision": control.revision + 1}),
        )

    host.announce.side_effect = announce
    async with asyncio.timeout(5):
        result = await maintenance.maintain_restart(host, [1])
    assert countdown_clock == [60]
    assert started.is_set()
    assert result == {1: {"status": "superseded"}}
    host.restart.assert_not_awaited()


@pytest.mark.parametrize("phase", ["announce", "sleep"])
async def test_cancelled_countdown_does_not_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    host = host_at(tmp_path)
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    monkeypatch.setenv("DST_MAINTENANCE_TASK", unit)
    monkeypatch.setenv(
        "DST_MAINTENANCE_REVISIONS",
        json.dumps({
            number: control_revision(host.rooms.path(number)) for number in (1, 2)
        }),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def sleep(_: float) -> None:
        entered.set()
        await release.wait()

    async def announce(*_: object) -> None:
        if phase == "announce":
            entered.set()
            await release.wait()

    host.announce.side_effect = announce
    api = SimpleNamespace(**vars(asyncio))
    api.sleep = sleep
    monkeypatch.setattr(maintenance, "asyncio", api)
    task = asyncio.create_task(maintenance.maintain_restart(host, [1, 2], delay=480))
    try:
        await wait_for_event(entered, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 5)
        host.restart.assert_not_awaited()
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    assert await maintenance.task_status(host, unit) == {
        "task": unit,
        "status": "cancelled",
        "rooms": {"1": {"status": "cancelled"}, "2": {"status": "cancelled"}},
    }


async def test_cancelled_restart_preserves_finished_inflight_and_queued_rooms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = host_at(tmp_path)
    for number in range(3, 14):
        host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
    numbers = range(1, 14)
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    monkeypatch.setenv("DST_MAINTENANCE_TASK", unit)
    monkeypatch.setenv(
        "DST_MAINTENANCE_REVISIONS",
        json.dumps({
            number: control_revision(host.rooms.path(number)) for number in numbers
        }),
    )
    control = read_control(host.rooms.path(3))
    write_control(
        host.rooms.path(3),
        control.model_copy(update={"revision": control.revision + 1}),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    inflight: set[int] = set()

    async def restart(number: int, **_: object) -> None:
        if number == 1:
            return
        if number == 2:
            msg = "restart failed"
            raise RuntimeError(msg)
        inflight.add(number)
        if len(inflight) == 8:
            entered.set()
        await release.wait()

    host.restart.side_effect = restart
    task = asyncio.create_task(maintenance.maintain_restart(host, numbers, delay=0))
    try:
        await wait_for_event(entered, task)
        assert inflight == set(range(4, 12))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 5)
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert [call.args[0] for call in host.restart.await_args_list] == [
        1,
        2,
        *range(4, 12),
    ]
    host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    assert await maintenance.task_status(host, unit) == {
        "task": unit,
        "status": "cancelled",
        "rooms": {
            "1": {"status": "restarted"},
            "2": {"status": "failed", "error": "restart failed"},
            "3": {"status": "superseded"},
            **{str(number): {"status": "indeterminate"} for number in range(4, 12)},
            "12": {"status": "cancelled"},
            "13": {"status": "cancelled"},
        },
    }


async def test_detached_task_reexecutes_cli_and_propagates_roots(
    tmp_path: Path,
) -> None:
    host = host_at(tmp_path)
    result = await maintenance.maintain_restart(host, [1, 2], detach=True)
    unit, argv = host.systemd.start_transient.await_args.args
    assert result == {
        "task": unit,
        "status": "submitted",
        "rooms": {1: {"status": "submitted"}, 2: {"status": "submitted"}},
    }
    assert maintenance.task_unit(unit) == unit
    assert argv[:3] == [sys.executable, "-m", "dst_server"]
    assert argv[argv.index("--cluster-root") + 1] == str(host.cluster_root)
    assert argv[argv.index("--quadlet-dir") + 1] == str(host.quadlet_dir)
    assert argv[argv.index("--room") + 1] == "1,2"
    assert "--detach" not in argv
    assert host.systemd.start_transient.await_args.kwargs["environment"] == {
        "DST_MAINTENANCE_TASK": unit,
        "DST_MAINTENANCE_REVISIONS": json.dumps({
            number: control_revision(host.rooms.path(number)) for number in (1, 2)
        }),
    }


async def test_stop_after_submission_invalidates_worker_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = host_at(tmp_path)
    await maintenance.maintain_restart(host, [1], detach=True)
    environment = host.systemd.start_transient.await_args.kwargs["environment"]
    control = read_control(host.rooms.path(1))
    write_control(
        host.rooms.path(1),
        control.model_copy(update={"revision": control.revision + 1}),
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert await maintenance.maintain_restart(host, [1], delay=0) == {
        1: {"status": "superseded"}
    }
    host.restart.assert_not_awaited()


@pytest.mark.parametrize(
    "encoded",
    ["", "{", "[]", "{}", '{"2": 0}', '{"1": -1}', '{"1": true}', '{"1": 1.0}'],
)
async def test_worker_rejects_missing_or_invalid_captured_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoded: str
) -> None:
    host = host_at(tmp_path)
    monkeypatch.setenv(
        "DST_MAINTENANCE_TASK", "dst-maintenance-" + "a" * 32 + ".service"
    )
    monkeypatch.setenv("DST_MAINTENANCE_REVISIONS", encoded)
    with pytest.raises(ValueError, match="maintenance revisions"):
        await maintenance.maintain_restart(host, [1], delay=0)
    host.restart.assert_not_awaited()


async def test_worker_sigterm_cancels_countdown_and_records_terminal_status(
    tmp_path: Path,
) -> None:
    host = host_at(tmp_path)
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    script = textwrap.dedent("""\
        import asyncio
        import signal
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from dst_server.host import maintenance
        from dst_server.rooms import RoomStore

        async def announce(*args):
            print("ANNOUNCED", flush=True)
            try:
                await asyncio.Event().wait()
            finally:
                print("ANNOUNCE_CLOSED", flush=True)

        async def restart(*args, **kwargs):
            raise AssertionError("cancelled countdown restarted a room")

        async def main():
            root = Path(sys.argv[1])
            host = SimpleNamespace(
                cluster_root=root,
                rooms=RoomStore(root),
                announce=announce,
                restart=restart,
            )
            previous = signal.getsignal(signal.SIGTERM)
            try:
                await maintenance.maintain_restart(host, [1], delay=480)
            except asyncio.CancelledError:
                assert signal.getsignal(signal.SIGTERM) == previous
                print("HANDLER_RESTORED", flush=True)
            else:
                raise AssertionError("SIGTERM did not cancel countdown")

        asyncio.run(main())
        """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(host.cluster_root),
        env={
            **os.environ,
            "DST_MAINTENANCE_TASK": unit,
            "DST_MAINTENANCE_REVISIONS": json.dumps({
                1: control_revision(host.rooms.path(1))
            }),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        async with asyncio.timeout(10):
            assert await process.stdout.readline() == b"ANNOUNCED\n"
            process.send_signal(signal.SIGTERM)
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().splitlines() == ["ANNOUNCE_CLOSED", "HANDLER_RESTORED"]
        host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
        assert await maintenance.task_status(host, unit) == {
            "task": unit,
            "status": "cancelled",
            "rooms": {"1": {"status": "cancelled"}},
        }
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.parametrize(
    "task",
    [
        "ssh.service",
        "../dst-maintenance-a",
        "dst-maintenance-a",
        "dst-maintenance-" + "a" * 33,
    ],
)
async def test_cancel_rejects_unrelated_services(tmp_path: Path, task: str) -> None:
    host = host_at(tmp_path)
    with pytest.raises(ValueError, match="invalid DST maintenance task"):
        await maintenance.cancel_task(host, task)
    host.systemd.stop.assert_not_awaited()


@pytest.mark.parametrize(
    "state", ["active", "activating", "deactivating", "failed", None]
)
async def test_task_status_and_cancel_use_systemd_without_results(
    tmp_path: Path, state: str | None
) -> None:
    host = host_at(tmp_path)
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    if state is None:
        host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    else:
        host.systemd.properties.return_value = {"ActiveState": state}
    expected: dict[str, object] = {
        "task": unit,
        "status": state or "finished-or-unavailable",
    }
    if state is not None:
        expected["properties"] = {"ActiveState": state}
    if state in {None, "failed"}:
        expected["rooms"] = {}
    assert await maintenance.task_status(host, unit) == expected
    assert await maintenance.cancel_task(host, unit) == {
        "task": unit,
        "status": "cancellation-requested",
    }
    host.systemd.stop.assert_awaited_once_with(unit)


@pytest.mark.parametrize("failure", ["missing-room", "control"])
async def test_initial_room_failure_does_not_block_other_restarts(
    tmp_path: Path, failure: str
) -> None:
    host = host_at(tmp_path)
    if failure == "missing-room":
        (host.rooms.path(1) / "cluster.ini").unlink()
    else:
        (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    result = await maintenance.maintain_restart(host, [1, 2], delay=0)
    assert result == {
        1: {"status": "failed", "error": ANY},
        2: {"status": "restarted"},
    }
    assert host.restart.await_args.args == (2,)


async def test_detached_snapshot_preserves_submission_failures_after_room_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = host_at(tmp_path)
    policy = read_control(host.rooms.path(1))
    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    submitted = await maintenance.maintain_restart(host, [1, 2], detach=True, delay=0)
    assert submitted == {
        "task": host.systemd.start_transient.call_args.args[0],
        "status": "submitted",
        "rooms": {
            1: {"status": "failed", "error": ANY},
            2: {"status": "submitted"},
        },
    }
    for key, value in host.systemd.start_transient.call_args.kwargs[
        "environment"
    ].items():
        monkeypatch.setenv(key, value)
    write_control(host.rooms.path(1), policy)
    result = await maintenance.maintain_restart(host, [1, 2], delay=0)
    assert result == {
        1: {"status": "failed", "error": ANY},
        2: {"status": "restarted"},
    }
    assert host.restart.await_args.args == (2,)
    host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    retained = await maintenance.task_status(
        host, host.systemd.start_transient.call_args.args[0]
    )
    assert retained["status"] == "failed"
    assert retained["rooms"] == {str(number): value for number, value in result.items()}


async def test_large_submission_errors_do_not_enter_the_worker_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = host_at(tmp_path)
    revision = control_revision(host.rooms.path(1))
    repaired = read_control(host.rooms.path(2))
    bad_policy = json.dumps({
        "schedule": "invalid schedule",
        "recycle": "invalid boolean",
        "paused": "invalid boolean",
        "revision": -1,
    })
    for number in range(300):
        if number != 1:
            directory = host.rooms.path(number)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "cluster.ini").touch()
            (directory / CONTROL_FILE).write_text(bad_policy)
    submitted = cast(
        dict[str, object],
        await maintenance.maintain_restart(host, range(300), detach=True),
    )
    assert len(json.dumps(submitted["rooms"]).encode()) > 128 * 1024
    environment = host.systemd.start_transient.call_args.kwargs["environment"]
    assert max(len(value.encode()) for value in environment.values()) < 8192
    revisions = json.loads(environment["DST_MAINTENANCE_REVISIONS"])
    assert revisions == {
        str(number): revision if number == 1 else None for number in range(300)
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", env={**os.environ, **environment}
    )
    try:
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    write_control(host.rooms.path(2), repaired)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    result = cast(
        dict[int, dict[str, object]],
        await maintenance.maintain_restart(host, range(300), delay=0),
    )
    assert result[1] == {"status": "restarted"}
    assert all(
        result[number]
        == {
            "status": "failed",
            "error": "room failed validation when maintenance was submitted",
        }
        for number in range(300)
        if number != 1
    )
    host.restart.assert_awaited_once_with(
        1, override=False, expected_revision=revision, timeout=10800
    )


async def test_no_background_task_is_submitted_when_every_room_failed(
    tmp_path: Path,
) -> None:
    host = host_at(tmp_path)
    for number in (1, 2):
        (host.rooms.path(number) / CONTROL_FILE).write_text("invalid JSON")
    result = await maintenance.maintain_restart(host, [1, 2], detach=True)
    assert result == {number: {"status": "failed", "error": ANY} for number in (1, 2)}
    host.systemd.start_transient.assert_not_awaited()


async def test_worker_room_corruption_records_failure_and_restarts_other_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = host_at(tmp_path)
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    monkeypatch.setenv("DST_MAINTENANCE_TASK", unit)
    monkeypatch.setenv(
        "DST_MAINTENANCE_REVISIONS",
        json.dumps({
            number: control_revision(host.rooms.path(number)) for number in (1, 2)
        }),
    )
    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    result = await maintenance.maintain_restart(host, [1, 2], delay=0)
    assert result == {
        1: {"status": "failed", "error": ANY},
        2: {"status": "restarted"},
    }
    host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    retained = await maintenance.task_status(host, unit)
    assert retained["status"] == "failed"
    assert "Invalid JSON" in retained["rooms"]["1"]["error"]


@pytest.mark.parametrize("collected", [False, True])
async def test_worker_retains_all_140_room_results_and_full_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collected: bool,
) -> None:
    host = host_at(tmp_path)
    for number in range(140):
        if number not in {1, 2}:
            host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    monkeypatch.setenv("DST_MAINTENANCE_TASK", unit)
    monkeypatch.setenv(
        "DST_MAINTENANCE_REVISIONS",
        json.dumps({
            number: control_revision(host.rooms.path(number)) for number in range(140)
        }),
    )
    if collected:
        host.systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    else:
        host.systemd.properties.return_value = {"ActiveState": "failed"}
    error = '错误\0"\\' * 10000

    def restart(number: int, **_: object) -> None:
        if number == 0:
            raise RuntimeError(error)

    host.restart.side_effect = restart
    await maintenance.maintain_restart(host, range(140), delay=0)
    restored = await maintenance.task_status(host, unit)
    assert restored == {
        "task": unit,
        "status": "failed",
        "rooms": {
            "0": {"status": "failed", "error": error},
            **{str(number): {"status": "restarted"} for number in range(1, 140)},
        },
    }
    path = host.cluster_root / ".dst-maintenance" / f"{unit}.json"
    assert json.loads(path.read_text()) == restored
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert host.restart.await_count == 140


@pytest.mark.parametrize(
    "relative",
    [
        "src/dst_server/host/systemd/dst-room-schedule.service",
        "deploy/systemd/dst-room-schedule.service",
    ],
)
def test_recycling_runs_after_both_successful_and_failed_schedule(
    relative: str,
) -> None:
    config = ConfigParser(interpolation=None)
    config.read(Path(__file__).parents[2] / relative)
    assert config["Unit"]["OnSuccess"] == "dst-room-recycle.service"
    assert config["Unit"]["OnFailure"] == "dst-room-recycle.service"


async def test_maintenance_does_not_parse_or_rewrite_dynamic_game_configuration(
    tmp_path: Path,
) -> None:
    host = host_at(tmp_path)
    revision = control_revision(host.rooms.path(1))
    world = host.rooms.path(1) / "forest/worldgenoverride.lua"
    world.write_text("return require('custom_world')\n")
    before = world.read_bytes()

    assert await maintenance.maintain_restart(host, [1], delay=0) == {
        1: {"status": "restarted"}
    }
    host.restart.assert_awaited_once_with(
        1, override=False, expected_revision=revision, timeout=10800
    )
    assert world.read_bytes() == before
