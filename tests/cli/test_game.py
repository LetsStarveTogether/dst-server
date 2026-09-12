import asyncio
import io
import sys
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import orjson
import pytest
from pydantic import SecretStr
from rich.text import Text
from ulid import ULID

from dst_server import commands as c
from dst_server.announcements import Countdown, Repeat
from dst_server.cli import game, main
from dst_server.cli import logs as cli_logs
from dst_server.errors import ErrorCode, ErrorInfo, IndeterminateError
from dst_server.host import Host
from dst_server.logs import (
    JournalLogs,
    JournalQuery,
    JournalRecord,
    JournalResult,
    JournalStream,
    NetdataLogQuery,
    NetdataLogResult,
    NetdataLogs,
)
from dst_server.models.cluster import ShardResult
from dst_server.models.console import ConsoleError, ConsoleResult, ConsoleValue
from dst_server.presets.lst import fleet_room
from dst_server.rooms import CONTROL_FILE
from dst_server.rpc import Subscription
from tests.helpers import wait_for_event


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            ("message=hello world", "paused=true", "count=2"),
            {"message": "hello world", "paused": True, "count": 2},
        ),
        (
            ("data=[1,null,false]", 'name="42"'),
            {"data": [1, None, False], "name": "42"},
        ),
    ],
)
def test_rpc_call_parses_fields_and_reaches_selected_endpoint(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    fields: tuple[str, ...],
    expected: dict[str, Any],
) -> None:
    client = rpc_clients[1]
    shard = client.shard.return_value
    shard.call.return_value = True
    arguments = [argument for field in fields for argument in ("-f", field)]
    assert (
        main([
            "--json",
            "rpc",
            "call",
            "future_method",
            "--room",
            "001",
            "--shard",
            "cave",
            "--timeout",
            "45",
            *arguments,
        ])
        == 0
    )
    assert orjson.loads(capsys.readouterr().out) == [
        {"room": 1, "ok": True, "result": True}
    ]
    client.shard.assert_called_once_with("cave")
    shard.call.assert_awaited_once_with("future_method", expected, timeout=45)
    client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    "fields",
    [("x=1", "x=2"), ("missing",), ("=missing",), ("x=NaN",), ('x={"a":1,"a":2}',)],
)
def test_invalid_rpc_fields_fail_before_connecting(
    rpc_clients: defaultdict[int, MagicMock],
    fields: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [argument for field in fields for argument in ("-f", field)]
    assert main(["rpc", "call", "future_method", "--room", "001", *arguments]) == 1
    assert not rpc_clients
    assert capsys.readouterr().err


def test_rpc_input_rejects_nonobject_and_field_collision(
    rpc_clients: defaultdict[int, MagicMock],
    tmp_path: Path,
) -> None:
    path = tmp_path / "input.json"
    arguments = ["rpc", "call", "future_method", "--room", "001", "--input", str(path)]
    path.write_text("[]")
    assert main(arguments) == 1
    path.write_text('{"count":1}')
    assert main([*arguments, "-f", "count=2"]) == 1
    assert not rpc_clients
    rpc_clients[1].call.return_value = True
    assert main([*arguments, "-f", "paused=true"]) == 0
    rpc_clients[1].call.assert_awaited_once_with(
        "future_method", {"count": 1, "paused": True}, timeout=None
    )
    rpc_clients[1].__aexit__.assert_awaited_once()


def test_console_source_validates_modes_and_preserves_multiline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = 'print("hi")\nreturn 42\n'
    path = tmp_path / "input.lua"
    path.write_text(text)
    assert game._source(None, path) == text
    with pytest.raises(ValueError, match="not both"):
        game._source("return 1", path)
    monkeypatch.setattr(game.sys, "stdin", io.StringIO(text))
    assert game._source(None, None) == text
    monkeypatch.setattr(game.sys, "stdin", io.StringIO(text))
    assert game._source(None, Path("-")) == text
    monkeypatch.setattr(game.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    with pytest.raises(ValueError, match="--interactive"):
        game._source(None, None)
    with pytest.raises(ValueError, match="empty"):
        game._source("  \n", None)


@pytest.mark.parametrize(
    ("arguments", "shard", "command"),
    [
        (["world", "save", "--timeout", "45"], None, c.ClusterSave(timeout=45)),
        (
            ["world", "save", "--shard", "cave", "--timeout", "46"],
            "cave",
            c.Save(timeout=46),
        ),
    ],
)
def test_game_command_executes_once_and_reports_json(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    shard: str | None,
    command: c.Request[Any],
) -> None:
    client = rpc_clients[1]
    assert main(["--json", *arguments, "--room", "001"]) == 0
    assert orjson.loads(capsys.readouterr().out) == [
        {"room": 1, "ok": True, "result": None}
    ]
    target = client if shard is None else client.shard.return_value
    target.invoke.assert_awaited_once_with(command)
    if shard is None:
        client.shard.assert_not_called()
    else:
        client.shard.assert_called_once_with(shard)
        client.invoke.assert_not_awaited()
    client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("failure", ["error", "unconfirmed"])
def test_pause_preserves_partial_shard_results_and_other_room_success(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    action: str,
    failure: str,
) -> None:
    error = ErrorInfo(ErrorCode.UNAVAILABLE, ULID(), "cave disconnected")
    rpc_clients[0].invoke.return_value = (
        ShardResult("forest", value=True),
        ShardResult("cave", error=error)
        if failure == "error"
        else ShardResult("cave", value=False),
    )
    rpc_clients[1].invoke.return_value = (
        ShardResult("forest", value=True),
        ShardResult("cave", value=True),
    )
    assert main(["--json", "world", action, "--room", "000,001"]) == 1
    output = capsys.readouterr()
    failed, succeeded = orjson.loads(output.out)
    assert failed["room"] == 0
    assert failed["ok"] is False
    assert failed["error"] == f"{action} was not confirmed by shards: cave"
    assert failed["result"] == [
        {"shard": "forest", "value": True, "error": None},
        {
            "shard": "cave",
            "value": None if failure == "error" else False,
            "error": {
                "code": "unavailable",
                "error_id": str(error.error_id),
                "message": "cave disconnected",
                "fields": [],
            }
            if failure == "error"
            else None,
        },
    ]
    assert succeeded == {
        "room": 1,
        "ok": True,
        "result": [
            {"shard": "forest", "value": True, "error": None},
            {"shard": "cave", "value": True, "error": None},
        ],
    }
    assert output.err == ""
    for client in rpc_clients.values():
        client.invoke.assert_awaited_once_with(c.ClusterPause(paused=action == "pause"))
        client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_single_shard_pause_requires_confirmation(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    action: str,
    confirmed: bool,
) -> None:
    client = rpc_clients[0]
    client.shard.return_value.invoke.return_value = confirmed
    assert main(["--json", "world", action, "--room", "000", "--shard", "cave"]) == int(
        not confirmed
    )
    (record,) = orjson.loads(capsys.readouterr().out)
    assert record["ok"] is confirmed
    assert record["result"] is confirmed
    if not confirmed:
        assert record["error"] == f"{action} was not confirmed by shard: cave"
    client.shard.assert_called_once_with("cave")
    client.shard.return_value.invoke.assert_awaited_once_with(
        c.Pause(paused=action == "pause")
    )
    client.__aexit__.assert_awaited_once()


def test_raw_rpc_preserves_false_confirmation_as_return_value(
    rpc_clients: defaultdict[int, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    target = rpc_clients[0].shard.return_value
    target.call.return_value = False
    assert (
        main([
            "--json",
            "rpc",
            "call",
            "pause",
            "--room",
            "000",
            "--shard",
            "cave",
            "-f",
            "paused=false",
        ])
        == 0
    )
    assert orjson.loads(capsys.readouterr().out) == [
        {"room": 0, "ok": True, "result": False}
    ]
    target.call.assert_awaited_once_with("pause", {"paused": False}, timeout=None)


def test_player_mutations_follow_current_shard_and_refuse_migrating_player(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = rpc_clients[1]
    client.get_player.return_value = SimpleNamespace(shard="cave")
    assert main(["--json", "player", "kick", "KU_example", "--room", "001"]) == 0
    assert orjson.loads(capsys.readouterr().out)[0]["ok"]
    client.shard.assert_called_once_with("cave")
    client.shard.return_value.invoke.assert_awaited_once_with(
        c.Kick(userid="KU_example")
    )
    client.status.assert_not_awaited()
    client.get_player.return_value = SimpleNamespace(shard=None)
    assert main(["--json", "player", "ban", "KU_example", "--room", "001"]) == 1
    result = orjson.loads(capsys.readouterr().out)[0]
    assert not result["ok"]
    assert "migrating" in result["error"]
    assert client.shard.return_value.invoke.await_count == 1
    assert client.__aexit__.await_count == 2


@pytest.mark.parametrize(
    ("terminal", "as_json"), [(True, False), (False, False), (True, True)]
)
def test_console_batch_preserves_output_errors_and_never_repeats_evaluation(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
    as_json: bool,
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    source = tmp_path / "input.lua"
    source.write_text('print("first line")\nreturn 42\n')
    rpc_clients[0].shard.return_value.evaluate.return_value = ConsoleResult(
        output="first line\nsecond line\n",
        values=(ConsoleValue(type="number", text="42"),),
    )
    rpc_clients[1].shard.return_value.evaluate.return_value = ConsoleResult(
        output="before failure\n",
        values=(),
        error=ConsoleError(kind="runtime", message="boom"),
    )
    assert (
        main([
            *(["--json"] if as_json else []),
            "console",
            "--file",
            str(source),
            "--room",
            "000-001",
            "--shard",
            "cave",
            "--timeout",
            "30",
        ])
        == 1
    )
    output = capsys.readouterr()
    if as_json or not terminal:
        records = orjson.loads(output.out)
        assert output.out.count("\n") == 1
        assert [record["room"] for record in records] == [0, 1]
        assert records[0]["ok"]
        assert records[0]["result"]["output"] == "first line\nsecond line\n"
        assert records[1]["result"]["error"] == {"kind": "runtime", "message": "boom"}
        assert not records[1]["ok"]
        assert output.err == ""
    else:
        assert (
            output.out
            == "Room 000\nfirst line\nsecond line\n42\nRoom 001\nbefore failure\n"
        )
        assert output.err == "runtime: boom\n"
    for client in rpc_clients.values():
        client.shard.return_value.evaluate.assert_awaited_once_with(
            source.read_text(), timeout=30
        )
        client.status.assert_not_awaited()
        client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("failed", [1, 9])
def test_partial_failure_preserves_other_rooms_and_never_retries(
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    failed: int,
) -> None:
    if failed == 1:
        rpc_clients[failed].invoke.side_effect = IndeterminateError()
    assert main(["--json", "world", "save", "--room", f"000,{failed:03d}"]) == 1
    records = orjson.loads(capsys.readouterr().out)
    assert records[0] == {"room": 0, "ok": True, "result": None}
    assert records[1]["room"] == failed
    assert not records[1]["ok"]
    if failed == 1:
        assert "indeterminate" in records[1]["error"]
    else:
        assert failed not in rpc_clients
    for client in rpc_clients.values():
        client.invoke.assert_awaited_once()
        client.__aexit__.assert_awaited_once()


def test_template_selection_excludes_other_rooms_and_mutations_require_targets(
    cli_host: Host,
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_host.rooms.save(
        fleet_room(2, token=SecretStr("test-token")).replace(template="forge")
    )
    assert main(["announce", "hello"]) == 1
    assert main(["announce", "hello", "--all", "--room", "000"]) == 1
    assert not rpc_clients
    assert (
        main(["--json", "announce", "hello", "--all", "--template", "pure_survival"])
        == 0
    )
    assert [record["room"] for record in orjson.loads(capsys.readouterr().out)] == [
        0,
        1,
    ]
    assert set(rpc_clients) == {0, 1}
    for client in rpc_clients.values():
        client.announce.assert_awaited_once_with(Repeat(message="hello"))
        client.__aexit__.assert_awaited_once()


def test_interactive_console_continues_after_lua_error_and_eof(
    rpc_clients: defaultdict[int, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    prompt = Mock()
    prompt.prompt_async = AsyncMock(side_effect=["broken()", "1+1", EOFError])
    monkeypatch.setattr("prompt_toolkit.PromptSession", lambda: prompt)
    target = rpc_clients[1].shard.return_value
    target.evaluate.side_effect = [
        ConsoleResult(
            output="", values=(), error=ConsoleError(kind="runtime", message="bad")
        ),
        ConsoleResult(output="", values=(ConsoleValue(type="number", text="2"),)),
    ]
    assert main(["console", "--interactive", "--room", "001", "--shard", "cave"]) == 0
    assert [call.args[0] for call in target.evaluate.await_args_list] == [
        "broken()",
        "1+1",
    ]
    output = capsys.readouterr()
    # prompt_toolkit displays interactive output and errors in one terminal stream.
    assert Text.from_ansi(output.out + output.err).plain == "runtime: bad\n2\n"
    rpc_clients[1].__aexit__.assert_awaited_once()


@pytest.mark.parametrize("ending", ["prompt_eof", "journal_error", "journal_eof"])
def test_interactive_follow_closes_prompt_and_journal_on_eof_or_log_failure(
    rpc_clients: defaultdict[int, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    prompt_started, journal_started = asyncio.Event(), asyncio.Event()
    prompt_closed, journal_closed = asyncio.Event(), asyncio.Event()

    async def records() -> AsyncIterator[None]:
        journal_started.set()
        async with asyncio.timeout(2):
            await prompt_started.wait()
            if ending == "journal_error":
                msg = "journal failed"
                raise OSError(msg)
            if ending == "journal_eof":
                return
            await asyncio.Event().wait()
        yield

    @asynccontextmanager
    async def journal(
        _: JournalLogs, units: Sequence[str], request: JournalQuery
    ) -> AsyncIterator[MagicMock]:
        assert units == ("dst-001-cave.service",)
        assert request.limit == 0
        assert request.direction == "forward"
        stream = MagicMock(spec=JournalStream)
        stream.__aiter__.side_effect = records
        stream.diagnostics = ""
        stream.diagnostics_truncated = False
        try:
            yield stream
        finally:
            journal_closed.set()

    async def read_prompt(_: str) -> str:
        prompt_started.set()
        try:
            async with asyncio.timeout(2):
                await journal_started.wait()
                if ending != "prompt_eof":
                    await asyncio.Event().wait()
        finally:
            prompt_closed.set()
        raise EOFError

    prompt = Mock(prompt_async=AsyncMock(side_effect=read_prompt))
    monkeypatch.setattr("prompt_toolkit.PromptSession", lambda: prompt)
    monkeypatch.setattr(JournalLogs, "follow", journal)
    assert main([
        "console",
        "--interactive",
        "--follow",
        "--room",
        "001",
        "--shard",
        "cave",
    ]) == int(ending == "journal_error")
    assert prompt_closed.is_set()
    assert journal_closed.is_set()
    rpc_clients[1].__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    "arguments",
    [
        ["console", "1+2", "--follow"],
        ["console", "1+2", "--interactive"],
        ["player", "admin", "--remove"],
    ],
)
def test_invalid_console_and_permission_modes_fail_before_connecting(
    rpc_clients: defaultdict[int, MagicMock],
    arguments: list[str],
) -> None:
    assert main([*arguments, "--room", "001"]) == 1
    assert not rpc_clients


@pytest.mark.parametrize("operation", ["diagnose", "logs", "shard_logs"])
def test_diagnostics_and_logs_work_when_room_policy_is_corrupted(
    cli_host: Host,
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    (cli_host.rooms.path(1) / CONTROL_FILE).write_text("{")
    selected: list[tuple[str, ...]] = []
    record = JournalRecord(
        fields={
            "__CURSOR": "test-cursor",
            "__REALTIME_TIMESTAMP": "1789171200000000",
            "_SYSTEMD_UNIT": "dst-001-cave.service",
            "MESSAGE": "game startup diagnostic",
        }
    )
    page = JournalResult(
        records=(record,),
        next_cursor=record.cursor,
        has_more=False,
        diagnostics="",
        diagnostics_truncated=False,
    )

    async def records(  # ruff: ignore[unused-async]
        _reader: JournalLogs,
        units: Sequence[str],
        request: JournalQuery,
        **_: object,
    ) -> JournalResult:
        selected.append(tuple(units))
        assert request.direction == "backward"
        return page

    monkeypatch.setattr(JournalLogs, "query", records)
    arguments = (
        ["room", "diagnose", "001"]
        if operation == "diagnose"
        else ["logs", "--room", "001"]
    )
    if operation == "shard_logs":
        arguments.extend(("--shard", "cave"))
    assert main(["--json", *arguments]) == 0
    output = orjson.loads(capsys.readouterr().out)
    if operation == "diagnose":
        assert output[0]["ok"] is True
        result = output[0]["result"]
        assert result["configuration_error"] == "room configuration could not be loaded"
        assert result["logs"] == page.model_dump(mode="json")
        assert set(result["units"]) == set(cli_host.units(1))
    else:
        assert output == page.model_dump(mode="json")
    expected = {"dst-001-cave.service"}
    if operation != "shard_logs":
        expected = {"dst-001-pod.service", "dst-001-*.service"}
    assert len(selected) == 1
    assert set(selected[0]) == expected
    assert not rpc_clients


@pytest.mark.parametrize("operation", ["rpc", "console"])
def test_debug_commands_work_when_room_policy_is_corrupted(
    cli_host: Host,
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    (cli_host.rooms.path(1) / CONTROL_FILE).write_text("{")
    client = rpc_clients[1]
    if operation == "rpc":
        client.describe.return_value = c.describe_operations("cluster")
        arguments = ["rpc", "list", "--room", "001"]
    else:
        client.shard.return_value.evaluate.return_value = ConsoleResult(
            output="", values=(ConsoleValue(type="number", text="2"),)
        )
        arguments = ["console", "1+1", "--room", "001", "--shard", "cave"]
    assert main(["--json", *arguments]) == 0
    (record,) = orjson.loads(capsys.readouterr().out)
    assert record["room"] == 1
    assert record["ok"] is True
    if operation == "rpc":
        assert "status" in {item["method"] for item in record["result"]}
        client.describe.assert_awaited_once_with()
    else:
        assert record["result"]["values"] == [{"type": "number", "text": "2"}]
        client.shard.assert_called_once_with("cave")
        client.shard.return_value.evaluate.assert_awaited_once_with("1+1", timeout=120)
    client.__aexit__.assert_awaited_once()


def test_rpc_subscriptions_all_start_and_one_failure_preserves_others(
    cli_host: Host,
    rpc_clients: defaultdict[int, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    active: set[int] = set()
    ready = asyncio.Event()
    subscriptions = []
    rpc_clients[0].subscribe.side_effect = ConnectionError("room offline")
    for number in range(1, 10):
        cli_host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
        subscription = MagicMock(spec=Subscription)
        subscription.closed = False
        subscription.__aenter__.return_value = subscription

        async def next_records(
            number: int = number, subscription: MagicMock = subscription
        ) -> tuple:
            active.add(number)
            if len(active) == 9:
                ready.set()
            async with asyncio.timeout(2):
                await ready.wait()
            subscription.closed = True
            return ()

        subscription.next.side_effect = next_records
        rpc_clients[number].subscribe.return_value = subscription
        subscriptions.append(subscription)
    assert main(["--json", "rpc", "subscribe", "events", "--all"]) == 1
    assert active == set(range(1, 10))
    assert orjson.loads(capsys.readouterr().out) == {
        "room": "000",
        "ok": False,
        "error": "room offline",
    }
    for subscription in subscriptions:
        subscription.__aexit__.assert_awaited_once()
    for client in rpc_clients.values():
        client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    ("terminal", "as_json"), [(True, False), (False, False), (True, True)]
)
def test_journal_page_keeps_metadata_and_displays_human_records_oldest_first(
    cli_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal: bool,
    as_json: bool,
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    records = tuple(
        JournalRecord(
            fields={
                "__CURSOR": f"cursor-{index}",
                "__REALTIME_TIMESTAMP": str(1789171200000000 + index),
                "_SYSTEMD_UNIT": "dst-299-retired.service",
                "MESSAGE": f"message-{index}",
            }
        )
        for index in (2, 1)
    )
    page = JournalResult(
        records=records,
        next_cursor="cursor-1",
        has_more=True,
        diagnostics="journal warning\n",
        diagnostics_truncated=True,
    )
    query = AsyncMock(return_value=page)
    monkeypatch.setattr(JournalLogs, "query", query)
    assert (
        main([
            *(["--json"] if as_json else []),
            "logs",
            "--room",
            "299",
            "--shard",
            "retired",
            "--lines",
            "2",
            "--direction",
            "backward",
            "--since",
            "-1h",
            "--until",
            "now",
            "--namespace",
            "games",
        ])
        == 0
    )
    assert not cli_host.rooms.path(299).exists()
    units, request = query.call_args.args
    assert units == ("dst-299-retired.service",)
    assert request == JournalQuery(
        limit=2,
        direction="backward",
        since="-1h",
        until="now",
        namespace="games",
    )
    output = capsys.readouterr()
    if as_json or not terminal:
        assert output.out.count("\n") == 1
        assert orjson.loads(output.out) == page.model_dump(mode="json")
    else:
        assert output.out.index("message-1") < output.out.index("message-2")
        assert "cursor-1" in output.err
        assert "More records" in output.err
    assert "journal warning" in output.err
    assert "diagnostics were truncated" in output.err


def test_journal_follow_emits_records_and_diagnostics_after_closing(
    cli_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = JournalRecord(
        fields={
            "__CURSOR": "next",
            "__REALTIME_TIMESTAMP": "1789171200000000",
            "_SYSTEMD_UNIT": "dst-299-retired.service",
            "MESSAGE": "follow output",
        }
    )
    stream = MagicMock(spec=JournalStream)
    stream.__aiter__.return_value = iter((record,))
    stream.diagnostics = ""
    stream.diagnostics_truncated = False
    closed = False

    @asynccontextmanager
    async def follow(
        _: JournalLogs, units: Sequence[str], request: JournalQuery
    ) -> AsyncIterator[MagicMock]:
        nonlocal closed
        assert units == ("dst-299-retired.service",)
        assert request == JournalQuery(limit=0, direction="forward", cursor="previous")
        try:
            yield stream
        finally:
            closed = True
            stream.diagnostics = "retained diagnostic\n"

    monkeypatch.setattr(JournalLogs, "follow", follow)
    assert (
        main([
            "--json",
            "logs",
            "--room",
            "299",
            "--shard",
            "retired",
            "--follow",
            "--lines",
            "0",
            "--cursor",
            "previous",
        ])
        == 0
    )
    assert closed
    assert not cli_host.rooms.path(299).exists()
    output = capsys.readouterr()
    assert orjson.loads(output.out) == record.model_dump(mode="json")
    assert output.err == "retained diagnostic\n"


def test_journal_follow_rejects_backward_direction_before_opening_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_host = Mock(side_effect=AssertionError("invalid query must not open Host"))
    monkeypatch.setattr(cli_logs, "make_host", make_host)
    assert (
        main([
            "logs",
            "--room",
            "001",
            "--follow",
            "--direction",
            "backward",
        ])
        == 1
    )
    assert "--follow requires --direction forward" in capsys.readouterr().err
    make_host.assert_not_called()


def test_telemetry_cli_queries_retired_room_without_assuming_a_service(
    cli_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    since, until = datetime(2026, 9, 12, tzinfo=UTC), datetime(2026, 9, 13, tzinfo=UTC)
    result = NetdataLogResult(
        records=(),
        matched=0,
        since=since,
        until=until,
        diagnostics="matched=0 returned=0\n",
        diagnostics_truncated=False,
    )
    query = AsyncMock(return_value=result)
    monkeypatch.setattr(NetdataLogs, "query", query)
    assert (
        main([
            "--json",
            "logs",
            "telemetry",
            "--room",
            "299",
            "--shard",
            "retired",
            "--since",
            since.isoformat(),
            "--until",
            until.isoformat(),
            "--limit",
            "12",
            "--filter",
            "severity_text=ERROR",
            "--query",
            "exception",
            "--field",
            "body",
        ])
        == 0
    )
    request = query.call_args.args[0]
    assert request == NetdataLogQuery(
        since=since,
        until=until,
        limit=12,
        query="exception",
        fields=("body",),
        filters=(
            ("attributes.dst.cluster.name", "dst-299"),
            ("attributes.dst.shard.name", "retired"),
            ("severity_text", "ERROR"),
        ),
    )
    assert not cli_host.rooms.path(299).exists()
    output = capsys.readouterr()
    assert orjson.loads(output.out) == result.model_dump(mode="json")
    assert output.err == result.diagnostics


@pytest.mark.parametrize(
    "arguments",
    [
        ["--since", "2026-09-12T00:00:00"],
        ["--since", "2026-09-12T00:00:00Z", "--filter", "bad"],
        ["--since", "2026-09-12T00:00:00Z", "--service-namespace", "games"],
    ],
)
def test_telemetry_cli_rejects_invalid_query_before_opening_host(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    make_host = Mock(side_effect=AssertionError("invalid query must not open Host"))
    monkeypatch.setattr(cli_logs, "make_host", make_host)
    assert main(["logs", "telemetry", "--room", "299", *arguments]) == 1
    make_host.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "announcement"),
    [
        (["hello"], Repeat(message="hello")),
        (
            ["hello", "--count", "3", "--interval", "15"],
            Repeat(message="hello", count=3, interval=15),
        ),
        (
            [
                "{room} opens in {remaining} seconds",
                "--countdown",
                "90",
                "--interval",
                "15",
                "--parameter",
                "room=Lobby",
            ],
            Countdown(
                template="{room} opens in {remaining} seconds",
                delay=90,
                interval=15,
                parameters={"room": "Lobby"},
            ),
        ),
    ],
)
def test_announcement_cli_delivers_validated_repeat_or_countdown(
    rpc_clients: defaultdict[int, MagicMock],
    arguments: list[str],
    announcement: Repeat | Countdown,
) -> None:
    assert main(["announce", *arguments, "--room", "001"]) == 0
    rpc_clients[1].announce.assert_awaited_once_with(announcement)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--count", "0"],
        ["--interval", "0"],
        ["--countdown", "-1"],
        ["--countdown", "10", "--count", "2"],
        ["--parameter", "room=Lobby"],
        ["--countdown", "10", "--parameter", "room=Lobby", "--parameter", "room=Cave"],
    ],
)
def test_invalid_announcement_never_connects_to_a_room(
    rpc_clients: defaultdict[int, MagicMock], arguments: list[str]
) -> None:
    assert main(["announce", "hello", *arguments, "--room", "001"]) != 0
    assert not rpc_clients


async def test_countdown_starts_in_all_rooms_before_any_countdown_finishes(
    cli_host: Host,
    rpc_clients: defaultdict[int, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    started = 0

    async def announce(plan: Countdown) -> None:
        nonlocal started
        assert isinstance(plan, Countdown)
        started += 1
        if started == 9:
            entered.set()
        await release.wait()

    for number in range(9):
        cli_host.rooms.save(fleet_room(number, token=SecretStr("test-token")))
        rpc_clients[number].announce.side_effect = announce
    monkeypatch.setattr(game, "make_host", lambda: cli_host)
    pending = asyncio.create_task(
        game.announce("{remaining} seconds", room=("000-008",), countdown=60)
    )
    try:
        async with asyncio.timeout(5):
            await wait_for_event(entered, pending)
            assert started == 9
            release.set()
            await pending
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
