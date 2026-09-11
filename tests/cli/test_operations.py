import asyncio
import json
from collections.abc import Sequence
from datetime import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from dst_server.cli import main
from dst_server.cli import operations as cli
from dst_server.cli.common import Options, context, options
from dst_server.host import Host, maintenance, schedule
from dst_server.presets.lst import fleet_room
from dst_server.rooms import CONTROL_FILE


@pytest.fixture
def host(cli_host: Host) -> Host:
    for number in range(2):
        cli_host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
    return cli_host


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("0", 0), ("8m", 480), ("1.5h", 5400), ("0.25s", 0.25), ("1e-05", 0.00001)],
)
def test_delay_parser_accepts_cli_and_detached_worker_values(
    value: str, seconds: float
) -> None:
    assert cli.duration(value) == seconds


@pytest.mark.parametrize("value", ["-1", "NaN", "inf", "1e9999", "1ms", " 8m", ""])
def test_delay_parser_rejects_invalid_and_nonfinite_values(value: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        cli.duration(value)


def test_schedule_windows_are_validated_before_edit_and_allow_midnight(
    host: Host, cli_systemd: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    before = host.rooms.load(0)
    for value in ("9:00-12:00", "09:00", "24:00-02:00", "09:00-09:00"):
        assert main(["schedule", "set", value, "--room", "000"]) == 1
        assert capsys.readouterr().err
    assert host.rooms.load(0) == before
    assert main(["schedule", "set", "09:00-12:00", "--room", "000", "--always"]) == 1
    assert "windows or --always" in capsys.readouterr().err
    assert (
        main([
            "--json",
            "schedule",
            "set",
            "09:00-12:00",
            "22:00-05:00",
            "--room",
            "000",
        ])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 1
    assert result[0]["room"] == 0
    assert result[0]["ok"]
    assert host.rooms.load(0).schedule[0].start == time(9)
    assert host.rooms.load(0).schedule[0].end == time(12)
    assert host.rooms.load(0).schedule[1].start == time(22)
    assert host.rooms.load(0).schedule[1].end == time(5)
    assert host.rooms.load(1).schedule == ()
    assert main(["schedule", "set", "--room", "000", "--always"]) == 0
    assert host.rooms.load(0).schedule == ()
    cli_systemd.start.assert_not_awaited()
    cli_systemd.stop.assert_not_awaited()


def test_pause_partial_failure_has_json_results_and_nonzero_exit(
    host: Host,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    previous = options.get()
    assert main(["--json", "schedule", "pause", "--room", "000-001"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"0", "1"}
    assert result["0"] == {"status": "paused"}
    assert result["1"]["status"] == "failed"
    assert "JSON" in result["1"]["error"]
    assert schedule.read_control(host.rooms.path(0)).paused
    assert (host.rooms.path(1) / CONTROL_FILE).read_text() == "invalid JSON"
    assert options.get() is previous


async def test_cancelled_maintenance_releases_host_and_has_no_extra_result(
    host: Host,
    cli_systemd: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entered = asyncio.Event()

    async def restart(active_host: Host, numbers: Sequence[int], **_: object) -> None:
        assert active_host.cluster_root == host.cluster_root
        assert numbers == (0,)
        assert active_host.systemd is cli_systemd
        entered.set()
        async with asyncio.timeout(2):
            await asyncio.Event().wait()

    monkeypatch.setattr(maintenance, "maintain_restart", restart)
    token = options.set(Options(host.cluster_root, host.quadlet_dir))
    task = asyncio.create_task(cli.maintenance_restart(room=("000",), delay="0"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        cli_systemd.aclose.assert_awaited_once()
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=1
            )
        finally:
            options.reset(token)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
async def test_restart_rejects_timeout_before_host_creation(
    monkeypatch: pytest.MonkeyPatch, value: float
) -> None:
    make_host = Mock(side_effect=AssertionError("invalid timeout must not open Host"))
    monkeypatch.setattr(cli, "make_host", make_host)
    with pytest.raises(ValueError, match=r"finite|greater than"):
        await cli.maintenance_restart(room=("000",), timeout=value)
    make_host.assert_not_called()


def test_detached_worker_command_reenters_same_cli_with_paths_and_timeout(
    host: Host,
    cli_systemd: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main([
            "--json",
            "maintenance",
            "restart",
            "--room",
            "000-001",
            "--delay",
            "0.00001s",
            "--detach",
            "--timeout",
            "12",
        ])
        == 0
    )
    cli_systemd.start_transient.assert_awaited_once()
    unit, argv = cli_systemd.start_transient.call_args.args
    assert json.loads(capsys.readouterr().out) == {
        "task": unit,
        "status": "submitted",
        "rooms": {"0": {"status": "submitted"}, "1": {"status": "submitted"}},
    }
    settings: list[Options] = []

    async def restart(  # ruff: ignore[unused-async]
        active_host: Host, numbers: Sequence[int], **kwargs: object
    ) -> dict[int, dict[str, str]]:
        settings.append(context())
        assert isinstance(active_host, Host)
        assert active_host.cluster_root == host.cluster_root
        assert active_host.quadlet_dir == host.quadlet_dir
        assert active_host.systemd is cli_systemd
        assert numbers == (0, 1)
        assert kwargs == {"delay": 0.00001, "detach": False, "timeout": 12.0}
        return {0: {"status": "restarted"}, 1: {"status": "restarted"}}

    monkeypatch.setattr(maintenance, "maintain_restart", restart)
    assert argv[1:3] == ["-m", "dst_server"]
    assert main(argv[3:]) == 0
    assert len(settings) == 1
    assert settings[0].cluster_root == host.cluster_root
    assert settings[0].quadlet_dir == host.quadlet_dir
    assert settings[0].json is True
    assert cli_systemd.aclose.await_count == 2


@pytest.mark.parametrize("command", [["master"], ["serve", "Caves"]])
def test_agent_port_validation_and_exit_code_without_launching_game(
    command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serve = AsyncMock(return_value=7)
    monkeypatch.setattr(cli, "_serve", serve)
    assert main(["agent", *command, "--external-port", "80"]) == 2
    serve.assert_not_awaited()
    assert main(["agent", *command, "--external-port", "10999"]) == 7
    serve.assert_awaited_once_with("Caves" if command[0] == "serve" else None, 10999)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_outputs_actual_shell_script(
    shell: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["completion", shell]) == 0
    output = capsys.readouterr().out
    assert "dst-server" in output
    assert "maintenance" in output
    assert "schedule" in output
    assert "external-port" in output


@pytest.mark.parametrize(
    "command",
    [
        ["agent", "prepare"],
        ["agent", "master"],
        ["agent", "serve"],
        ["schedule", "show"],
        ["schedule", "set"],
        ["maintenance", "restart"],
        ["maintenance", "status"],
        ["annotations"],
    ],
)
def test_operation_help_does_not_open_host(
    command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_host = Mock(side_effect=AssertionError("help must not open Host"))
    monkeypatch.setattr(cli, "make_host", make_host)
    assert main([*command, "--help"]) == 0
    make_host.assert_not_called()


def test_annotations_runs_from_root_cli_and_preserves_exit_status(
    tmp_path: Path,
) -> None:
    source = tmp_path / "modutil.lua"
    source.write_text("env.Example = function(value) return true end\n")
    output = tmp_path / "annotations.lua"
    assert main(["annotations", str(source), "--output", str(output)]) == 0
    assert "function Example" in output.read_text()
    previous = output.read_text()
    source.write_text("-- no definitions\n")
    assert main(["annotations", str(source), "--output", str(output)]) == 1
    assert output.read_text() == previous


def test_detached_partial_failure_reports_submitted_task_and_fails_exit(
    host: Host, cli_systemd: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    assert main(["--json", "maintenance", "restart", "--all", "--detach"]) == 1
    result = json.loads(capsys.readouterr().out)
    cli_systemd.start_transient.assert_awaited_once()
    assert result["task"] == cli_systemd.start_transient.call_args.args[0]
    assert result["rooms"]["0"]["status"] == "submitted"
    assert result["rooms"]["1"]["status"] == "failed"


@pytest.mark.parametrize("failed", [False, True])
def test_worker_retains_results_for_status_and_preserves_failure_exit(
    host: Host,
    cli_systemd: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed: bool,
) -> None:
    unit = "dst-maintenance-" + "a" * 32 + ".service"
    monkeypatch.setenv("DST_MAINTENANCE_TASK", unit)
    monkeypatch.setenv(
        "DST_MAINTENANCE_REVISIONS",
        json.dumps({number: host.rooms.policy(number).revision for number in (0, 1)}),
    )
    assert host.rooms.numbers() == (0, 1)
    restart = AsyncMock(
        side_effect=[RuntimeError("game failed") if failed else None, None]
    )
    monkeypatch.setattr(Host, "restart", restart)
    assert main(["--json", "maintenance", "restart", "--all", "--delay", "0"]) == int(
        failed
    )
    rooms = {
        "0": {"status": "failed", "error": "game failed"}
        if failed
        else {"status": "restarted"},
        "1": {"status": "restarted"},
    }
    assert json.loads(capsys.readouterr().out) == rooms
    retained = {
        "task": unit,
        "status": "failed" if failed else "completed",
        "rooms": rooms,
    }
    path = host.cluster_root / ".dst-maintenance" / f"{unit}.json"
    assert json.loads(path.read_text()) == retained
    cli_systemd.properties.side_effect = RuntimeError("NoSuchUnit")
    assert main(["--json", "maintenance", "status", unit]) == 0
    assert json.loads(capsys.readouterr().out) == retained
    assert restart.await_count == 2


def test_explicit_selection_isolates_missing_room_from_healthy_room(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "schedule", "pause", "--room", "0,2"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["0"]["status"] == "paused"
    assert schedule.read_control(host.rooms.path(0)).paused
    assert result["2"]["status"] == "failed"


def test_template_filter_reports_corrupt_candidate(
    host: Host,
) -> None:
    from dst_server.cli.common import select_rooms

    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    with pytest.raises(ValueError, match="cannot filter room 001 by template"):
        select_rooms(host, template="pure_survival")


def test_lifecycle_and_schedule_use_policy_without_parsing_dynamic_lua(
    host: Host, cli_systemd: Mock
) -> None:
    path = host.rooms.path(0) / "forest" / "worldgenoverride.lua"
    source = 'return { override_enabled = true, preset = os.getenv("WORLD_PRESET") }\n'
    path.write_text(source)
    assert (
        main([
            "room",
            "start",
            "0",
            "--template",
            "pure_survival",
            "--no-wait",
        ])
        == 0
    )
    assert (
        main([
            "schedule",
            "set",
            "09:00-12:00",
            "--room",
            "0",
        ])
        == 0
    )
    assert host.rooms.policy(0).schedule[0].start == time(9)
    assert main(["schedule", "pause", "--room", "0"]) == 0
    assert host.rooms.policy(0).paused
    assert main(["room", "stop", "0", "--no-wait"]) == 0
    assert path.read_text() == source
    cli_systemd.start.assert_awaited_once_with(host.unit(0))
    cli_systemd.stop.assert_awaited_once_with(host.unit(0))
