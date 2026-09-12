import asyncio
from collections.abc import Sequence
from datetime import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from pydantic import SecretStr

from dst_server.cli import main
from dst_server.cli import operations as cli
from dst_server.cli.common import Options, options
from dst_server.host import Host, maintenance
from dst_server.presets.lst import fleet_room
from dst_server.rooms import CONTROL_FILE, read_control


@pytest.fixture
def host(cli_host: Host) -> Host:
    for number in range(2):
        cli_host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
    return cli_host


def test_manual_agent_prepare_updates_when_automatic_updates_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dst_server.cluster import service

    prepare = AsyncMock()
    monkeypatch.setattr(service, "prepare_shared", prepare)
    monkeypatch.setenv("DST_SERVER_MOD_AUTO_UPDATE", "false")
    assert main(["agent", "prepare"]) == 0
    prepare.assert_awaited_once_with(update_mods=True)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("0", 0), ("8m", 480), ("1.5h", 5400), ("0.25s", 0.25), ("1e-05", 0.00001)],
)
def test_delay_parser_accepts_seconds_and_units(value: str, seconds: float) -> None:
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
    result = orjson.loads(capsys.readouterr().out)
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


def test_schedule_edits_require_stopped_rooms(
    host: Host, cli_systemd: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["room", "start", "0", "--no-wait"]) == 0
    before = host.rooms.policy(0)
    cli_systemd.start.reset_mock()
    command = ["schedule", "set", "09:00-12:00", "--room", "0"]
    capsys.readouterr()
    assert main(["--json", *command]) == 1
    (record,) = orjson.loads(capsys.readouterr().out)
    assert "stopped room" in record["error"]
    assert host.rooms.policy(0) == before
    cli_systemd.stop.assert_not_awaited()

    assert main(["room", "stop", "0"]) == 0
    cli_systemd.stop.reset_mock()
    assert main(command) == 0
    assert host.rooms.policy(0).schedule[0].start == time(9)
    cli_systemd.start.assert_not_awaited()
    cli_systemd.stop.assert_not_awaited()


def test_pause_partial_failure_has_json_results_and_nonzero_exit(
    host: Host,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    previous = options.get()
    assert main(["--json", "schedule", "pause", "--room", "000-001"]) == 1
    result = orjson.loads(capsys.readouterr().out)
    assert set(result) == {"0", "1"}
    assert result["0"] == {"status": "paused"}
    assert result["1"]["status"] == "failed"
    assert "JSON" in result["1"]["error"]
    assert read_control(host.rooms.path(0)).paused
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


def test_explicit_selection_isolates_missing_room_from_healthy_room(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "schedule", "pause", "--room", "0,2"]) == 1
    result = orjson.loads(capsys.readouterr().out)
    assert result["0"]["status"] == "paused"
    assert read_control(host.rooms.path(0)).paused
    assert result["2"]["status"] == "failed"


def test_template_filter_reports_corrupt_candidate(
    host: Host,
) -> None:
    from dst_server.cli.common import select_rooms

    (host.rooms.path(1) / CONTROL_FILE).write_text("invalid JSON")
    with pytest.raises(ValueError, match="cannot filter room 001 by template"):
        select_rooms(host, template="pure_survival")


def test_lifecycle_and_schedule_controls_do_not_parse_dynamic_lua(
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
    assert main(["schedule", "show", "--room", "0"]) == 0
    assert main(["schedule", "pause", "--room", "0"]) == 0
    assert host.rooms.policy(0).paused
    assert main(["room", "stop", "0", "--no-wait"]) == 0
    assert path.read_text() == source
    cli_systemd.start.assert_awaited_once_with(host.unit(0))
    cli_systemd.stop.assert_awaited_once_with(host.unit(0))


def test_room_restart_calls_host(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    operation = AsyncMock(return_value={"number": 0, "action": "restart"})
    monkeypatch.setattr(Host, "restart", operation)
    assert main(["room", "restart", "000"]) == 0
    operation.assert_awaited_once_with(0, wait=True, timeout=10800.0)
    assert host.rooms.numbers() == (0, 1)
