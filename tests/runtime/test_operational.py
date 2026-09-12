import asyncio
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from ulid import ULID

from dst_server.events.server import SavedEvent
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime import server as server_module
from dst_server.runtime.console import Console
from tests.helpers import wait_for_event
from tests.runtime.helpers import StubWriter, feed_response, next_request


@pytest.mark.parametrize("kind", ["log", "stats", "log-then-stats"])
async def test_idle_stdout_releases_large_temporary_lines(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    server = observed_server(ServerConfig(shard="test"), log_handler=lambda _: None)
    reader = asyncio.StreamReader(limit=server_module.SUBPROCESS_STREAM_LIMIT)
    idle = asyncio.Event()
    read_line = server_module.read_line
    read_count = 0
    expected_lines = 2 if kind == "log-then-stats" else 1

    async def read() -> tuple[bytes | None, bool]:
        nonlocal read_count
        if read_count == expected_lines:
            idle.set()
        read_count += 1
        return await read_line(reader)

    monkeypatch.setattr(server_module, "read_line", lambda _: read())
    pumping = asyncio.create_task(server.pump_logs(reader))
    was_tracing = tracemalloc.is_tracing()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    try:
        reader.feed_data(
            (b"DST_Stats|" if kind == "stats" else b"[00:00:01]: ")
            + b"x" * 1_000_000
            + b"\n"
            + (b"DST_Stats|1\n" if kind == "log-then-stats" else b"")
        )
        await wait_for_event(idle, pumping)
        if pumping.done():
            pumping.result()
            pytest.fail("stdout pump exited before EOF")
        # Count actual retained allocations, including containers and helper frames.
        assert tracemalloc.get_traced_memory()[0] - baseline < 250_000
        reader.feed_eof()
        await asyncio.wait_for(pumping, timeout=5)
    finally:
        async with asyncio.timeout(5):
            if not was_tracing:
                tracemalloc.stop()
            reader.feed_eof()
            pumping.cancel()
            await asyncio.gather(pumping, return_exceptions=True)
            await server.finish()


def observed_server(*args: Any, **kwargs: Any) -> Server:
    server = Server(*args, **kwargs)
    server.recorder.observe_log = Mock(wraps=server.recorder.observe_log)
    return server


def observations(server: Server) -> list[Any]:
    observe = server.recorder.observe_log
    assert isinstance(observe, Mock)
    return [
        SimpleNamespace(uid=call.kwargs["attributes"]["log.record.uid"], **call.kwargs)
        for call in observe.call_args_list
        if not call.kwargs["event_name"].startswith("dst.telemetry.")
        and call.kwargs["event_name"] != "dst.world.state_changed"
    ]


async def capture_stdout(*lines: str) -> tuple[list[Any], list[str]]:
    logs: list[str] = []
    server = observed_server(ServerConfig(shard="test"), log_handler=logs.append)
    reader = asyncio.StreamReader()
    reader.feed_data("\n".join((*lines, "")).encode())
    reader.feed_eof()
    await server.pump_logs(reader)
    await server.finish()
    return observations(server), logs


@pytest.mark.parametrize(
    ("line", "event", "body", "severity"),
    [
        (
            (
                "CURL ERROR: (dst.metrics.klei.com) [28]"
                "Resolving timed out after 5001 milliseconds"
            ),
            "dst.runtime.diagnostic",
            {"kind": "network_error", "service": "metrics", "code": 28},
            "WARN",
        ),
        (
            "[Warning] Authentication denied for '76561198000000000':  (11)",
            "dst.runtime.diagnostic",
            {"kind": "authentication_denied", "code": 11},
            "WARN",
        ),
        (
            "DownloadServerMods timed out with no response from Workshop...",
            "dst.runtime.diagnostic",
            {"kind": "workshop_timeout"},
            "ERROR",
        ),
        (
            "LUA ERROR stack traceback:",
            "dst.runtime.diagnostic",
            {"kind": "lua_error"},
            "ERROR",
        ),
        (
            "Error loading mod: workshop-123456 (Example Mod)!",
            "dst.runtime.diagnostic",
            {"kind": "mod_load_error", "mod_id": "123456"},
            "ERROR",
        ),
        (
            (
                "[Warning] cSteamPunchthrough::onAuthenticationApproved"
                " - Client already authenticated"
            ),
            "dst.runtime.diagnostic",
            {
                "kind": "authentication_state_error",
                "operation": "approved",
                "reason": "already_authenticated",
            },
            "WARN",
        ),
        (
            (
                "[Error] cSteamPunchthrough::onAuthenticationDenied"
                " - Missing client object"
            ),
            "dst.runtime.diagnostic",
            {
                "kind": "authentication_state_error",
                "operation": "denied",
                "reason": "missing_client",
            },
            "ERROR",
        ),
        (
            "An error occured during world gen we will retry! [was \t1\t of \t5\t]\t",
            "dst.runtime.diagnostic",
            {"kind": "worldgen_retry", "attempt": 1, "max_attempts": 5},
            "WARN",
        ),
        (
            "An error occured during world and we give up! [was \t5\t of \t5\t]\t",
            "dst.runtime.diagnostic",
            {"kind": "worldgen_failed", "attempt": 5, "max_attempts": 5},
            "ERROR",
        ),
        (
            (
                "ERROR: wagpunk_arena_manager expected to be able to calculate the set "
                "piece angle using hermitcrab_marker and beebox_hermit "
                "but found neither of these.\t"
            ),
            "dst.runtime.diagnostic",
            {"kind": "setpiece_angle_unavailable", "prefab": "wagpunk_arena_manager"},
            "ERROR",
        ),
        (
            (
                "ERROR: hermitcrab_relocation_manager expected to be able to calculate "
                "the set piece angle using monkeyqueen and monkeyportal "
                "but found neither of these.\t"
            ),
            "dst.runtime.diagnostic",
            {
                "kind": "setpiece_angle_unavailable",
                "prefab": "hermitcrab_relocation_manager",
            },
            "ERROR",
        ),
    ],
)
@pytest.mark.parametrize("timestamp", ["", "[03:32:41]: "])
async def test_native_diagnostics_have_only_safe_structured_fields(
    line: str,
    event: str,
    body: dict[str, Any],
    severity: str,
    timestamp: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "time_ns", lambda: 1_234_567_890)
    records, logs = await capture_stdout(timestamp + line)

    assert len(records) == 1
    record = records[0]
    assert record.event_name == event
    assert record.body == body
    assert record.severity_text == severity
    assert record.observed_timestamp_ns == 1_234_567_890
    assert str(ULID.from_str(record.uid)) == record.uid
    assert logs == [timestamp + line]


@pytest.mark.parametrize(
    "line",
    [
        "[Say] (KU_private) Player: LUA ERROR stack traceback:",
        "[Whisper] (KU_private) Player: CURL ERROR: (secret.example) [28]token=secret",
        "Server Paused token=secret",
        "console: Error loading mod: workshop-123456 (secret)!",
        "print('Server Paused')",
        "token=secret LUA ERROR stack traceback:",
        "ERROR: private arbitrary application data",
        "Mod: workshop-123456 (Example)  Mod had no modworldgenmain.lua. Skipping.",
        "[Workshop] DownloadPublishedFile [8] 123456",
        (
            "[Warning] cSteamPunchthrough::onAuthenticationDenied"
            " - Missing client object with token=secret"
        ),
        "An error occured during world and we give up! token=secret",
    ],
)
async def test_arbitrary_stdout_and_player_text_are_not_operational_logs(
    line: str,
) -> None:
    records, logs = await capture_stdout("[00:00:01]: " + line)

    assert records == []
    assert logs == ["[00:00:01]: " + line]


async def test_network_diagnostic_never_exports_host_or_error_details() -> None:
    records, _ = await capture_stdout(
        "CURL ERROR: (token-secret.internal) [7]"
        "Failed to connect to 192.0.2.1:1080 with token=secret and KU_private"
    )

    assert len(records) == 1
    assert records[0].body == {
        "kind": "network_error",
        "service": "other",
        "code": 7,
    }
    encoded = orjson.dumps(records[0].body).decode()
    for private in ("secret", "internal", "192.0.2.1", "KU_private"):
        assert private not in encoded


# Short source signatures and their process origins: README.md#log-boundaries.
# Local names and the current CURL format are synthesized from build 747465.
@pytest.mark.parametrize(
    ("corpus", "diagnostics"),
    [
        pytest.param(
            "Mod: local-test Error loading mod!\n"
            "module 'screens/loadgamescreen' not found\n"
            "LUA ERROR stack traceback:\n\t=[C] in function 'require'",
            [({"kind": "lua_error"}, "ERROR")],
            id="lua-traceback",
        ),
        pytest.param(
            "Mod: local-test Mod had no modworldgenmain.lua. Skipping.",
            [],
            id="optional-mod-file",
        ),
        pytest.param(
            "DownloadServerMods timed out with no response from Workshop...",
            [({"kind": "workshop_timeout"}, "ERROR")],
            id="game-workshop",
        ),
        pytest.param(
            "ERROR! Download item 375850593 failed (Timeout).",
            [],
            id="steamcmd-lookalike",
        ),
        pytest.param(
            "PANIC: Too many disconnected tiles...\n"
            "An error occured during world gen we will retry! [was 1 of 5 ]\n"
            "An error occured during world and we give up! [was 5 of 5 ]",
            [
                ({"kind": "worldgen_retry", "attempt": 1, "max_attempts": 5}, "WARN"),
                ({"kind": "worldgen_failed", "attempt": 5, "max_attempts": 5}, "ERROR"),
            ],
            id="worldgen",
        ),
        pytest.param(
            "[S_API FAIL] SteamAPI_Init() failed; SteamAPI_IsSteamRunning() failed.\n"
            "Segmentation fault (core dumped)",
            [],
            id="native-and-supervisor",
        ),
        pytest.param(
            "./dontstarve_dedicated_server_nullrenderer_x64: error while loading "
            "shared libraries: libcurl-gnutls.so.4: cannot open shared object file: "
            "No such file or directory",
            [],
            id="loader-stderr",
        ),
        pytest.param(
            '[200] Account Failed (6): "E_INVALID_TOKEN"\n'
            "No auth token could be found.\n"
            "CURL ERROR: (lobby.c.kleientertainment.com) Could not resolve host: "
            "lobby.c.kleientertainment.com; Unknown error\n"
            "CURL ERROR: (lobby-v2.klei.com) [6]Could not resolve host",
            [({"kind": "network_error", "service": "lobby", "code": 6}, "WARN")],
            id="auth-and-dns",
        ),
        pytest.param(
            "CreateBoundSocket: ::bind couldn't find an open port between "
            "27016 and 27016",
            [],
            id="bind-attempt",
        ),
    ],
)
@pytest.mark.parametrize("prefix", [b"", b"[00:00:01]: ", b"[125:59:59]: "])
@pytest.mark.parametrize("framing", ["lf", "crlf", "eof"])
@pytest.mark.parametrize("fragmented", [False, True], ids=["whole", "split-reads"])
@pytest.mark.parametrize("damage", ["utf8", "interleaved", "physical-newline"])
async def test_mixed_byte_stream_preserves_log_and_event_boundaries(
    corpus: str,
    diagnostics: list[tuple[dict[str, Any], str]],
    prefix: bytes,
    framing: str,
    fragmented: bool,
    damage: str,
) -> None:
    logs: list[str] = []
    server = observed_server(ServerConfig(shard="test"), log_handler=logs.append)
    # Includes the actual beefalo/abigail emoji code points from emoji_items.lua.
    name = "测试\U000f0001\U000f001c👩🏽\u200d💻❤️e\u0301\u200b\u202eRTL\u202c"
    name += "\u0085\u2028\u2029"
    first, last = [
        b"DST_OTEL|"
        + orjson.dumps(
            {
                "v": 2,
                "nonce": server.game_events.nonce,
                "generation": 1,
                "session_id": name,
                "seq": sequence,
                "event": "dst.world.state_changed",
                "tick": 10,
                "monotonic_ms": 20,
                "cycle": 2,
                "data": {"name": "cycles", "value": 2},
            },
        )
        for sequence in (1, 2)
    ]
    ordinary = [line.encode() for line in corpus.split("\n")]
    ordinary += [
        b"[Say] (KU_TEST) " + name.encode() + b": " + first,
        b"#LUA ERROR: " + first,
        b"native error: \xff\x00\xfe " + name.encode(),
        b"DST_Master_Ready",
        b"DST_SessionId|fake-session",
        b"DST_Saved|session/fake/99",
        b"DST_Stopping",
        b"DST_Shutdown",
        b"DST_SERVER_FRAME|" + server.game_events.nonce.encode() + b"|START",
        b"DST_SERVER_RESULT|{}",
        b"DST_SERVER_FRAME|" + server.game_events.nonce.encode() + b"|END",
        b"DST_RemoteCommandDone",
    ]
    expected_logs = [(prefix + line).decode(errors="replace") for line in ordinary]
    if damage == "utf8":
        damaged = [first.replace(name.encode(), b"\xed\xa0\x80")]
    elif damage == "interleaved":
        damaged = [first[:20] + b"LUA ERROR stack traceback:" + first[20:]]
    else:
        damaged = [first[:20], first[20:]]
        expected_logs.append((prefix + damaged[1]).decode())
    separator = b"\r\n" if framing == "crlf" else b"\n"
    payload = separator.join(
        prefix + line
        for line in [first, *ordinary, b"DST_Stats|1|2|3|4|5", *damaged, last]
    )
    if framing != "eof":
        payload += separator

    reader = asyncio.StreamReader()
    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(server.pump_logs(reader))
        # Split the marker, a four-byte scalar, and the physical line terminator.
        cuts = (
            sorted({
                payload.index(b"DST_OTEL|") + 5,
                payload.index("\U000f0001".encode()) + 2,
                payload.index(separator) + 1,
                len(payload) // 2,
                len(payload) - 1,
                len(payload),
            })
            if fragmented
            else [len(payload)]
        )
        start = 0
        for stop in cuts:
            reader.feed_data(payload[start:stop])
            start = stop
            await asyncio.sleep(0)
        reader.feed_eof()
    await server.finish()

    assert logs == expected_logs
    assert server.telemetry_invalid == 1
    assert server.telemetry_dropped == 0
    events = []
    while event := await server.read_game_event():
        events.append(event.record)
    assert [event.seq for event in events] == [1, 2]
    assert all(event.session_id == name for event in events)
    records = observations(server)
    assert [(record.body, record.severity_text) for record in records] == diagnostics
    assert all(record.event_name == "dst.runtime.diagnostic" for record in records)
    assert await server.read_event() is None
    assert server.lifecycle.session_id is None
    assert not server.lifecycle.ready
    assert not server.lifecycle.stopping.is_set()


async def test_native_lifecycle_is_projected_once_without_raw_paths_or_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "time_ns", lambda: 321)
    server = observed_server(ServerConfig(shard="test"))
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"DST_Master_Ready|token=secret\nDST_SessionId|ABC123\n"
        b"DST_Saved|/private/secret/session/ABC123/27\nDST_Stopping\nDST_Shutdown\n"
    )
    reader.feed_eof()
    await server._pump_lifecycle(reader)
    await server.finish()
    records = observations(server)

    assert [(record.event_name, record.body) for record in records] == [
        ("dst.server.ready", {}),
        ("dst.server.session", {"session_id": "ABC123"}),
        ("dst.server.saved", {"snapshot": 27}),
        ("dst.server.stopping", {}),
        ("dst.server.shutdown", {}),
    ]
    assert len({record.uid for record in records}) == len(records)
    assert all(record.severity_text == "INFO" for record in records)
    assert "secret" not in orjson.dumps([record.body for record in records]).decode()
    lifecycle = []
    while (event := await server.read_event()) is not None:
        lifecycle.append(event.event)
    assert lifecycle == ["ready", "session", "saved", "stopping", "shutdown"]


async def test_stats_are_discarded_in_both_native_output_paths() -> None:
    logs: list[str] = []
    server = observed_server(ServerConfig(shard="test"), log_handler=logs.append)
    stats = b"DST_Stats|1|2|3|4|5\n" * 5000
    stdout = asyncio.StreamReader()
    stdout.feed_data(stats + b"ordinary log\n")
    stdout.feed_eof()
    native = asyncio.StreamReader()
    native.feed_data(b"DST_Saved|session/ABC123/27\n" + stats)
    native.feed_eof()
    await asyncio.gather(server.pump_logs(stdout), server._pump_lifecycle(native))
    await server.finish()

    assert logs == ["ordinary log"]
    assert isinstance(await server.read_event(), SavedEvent)
    assert await server.read_event() is None
    assert [record.event_name for record in observations(server)] == [
        "dst.server.saved"
    ]


async def test_operational_ingestion_has_no_subscriber_queue_limit() -> None:
    server = observed_server(ServerConfig(shard="test"), log_handler=lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(b"LUA ERROR stack traceback:\n" * 1100)
    reader.feed_eof()
    await asyncio.wait_for(server.pump_logs(reader), 3)
    await server.finish()

    records = observations(server)
    assert len(records) == 1100
    assert all(record.body == {"kind": "lua_error"} for record in records)


async def test_physical_line_loss_is_reported_without_retaining_oversized_input() -> (
    None
):
    server = observed_server(ServerConfig(shard="test"), log_handler=lambda _: None)
    reader = asyncio.StreamReader(limit=server_module.SUBPROCESS_STREAM_LIMIT)
    reader.feed_data(b"private" * 200_000 + b"\nLUA ERROR stack traceback:\n")
    reader.feed_eof()
    await server.pump_logs(reader)
    await server.finish()

    observe = server.recorder.observe_log
    assert isinstance(observe, Mock)
    reports = [call.kwargs for call in observe.call_args_list]
    assert [report["event_name"] for report in reports] == [
        "dst.telemetry.physical_line_oversized",
        "dst.runtime.diagnostic",
    ]
    assert reports[0]["body"]["count"] == 1
    assert reports[0]["body"]["limit_bytes"] == server_module.SUBPROCESS_STREAM_LIMIT
    assert "private" not in orjson.dumps(reports).decode()


async def test_process_exit_drains_both_pipe_tails_before_observation_eof() -> None:
    server = observed_server(ServerConfig(shard="test"), log_handler=lambda _: None)
    process = Mock(pid=42, returncode=6, wait=AsyncMock(return_value=6))
    server.child = cast("asyncio.subprocess.Process", process)
    native = asyncio.StreamReader()
    native.feed_data(b"DST_Saved|session/ABC123/28\n")
    native.feed_eof()
    stdout = asyncio.StreamReader()
    stdout.feed_data(b"LUA ERROR stack traceback:")
    stdout.feed_eof()
    server.lifecycle_task = asyncio.create_task(server._pump_lifecycle(native))
    server.log_task = asyncio.create_task(server.pump_logs(stdout))

    assert await server.wait() == 6
    assert await server.wait() == 6
    records = observations(server)
    assert sorted(record.event_name for record in records[:-1]) == [
        "dst.runtime.diagnostic",
        "dst.server.saved",
    ]
    assert records[-1].event_name == "dst.server.process_exited"
    assert records[-1].body == {"returncode": 6, "requested": False}
    assert records[-1].severity_text == "ERROR"
    assert await server.read_event() == SavedEvent(
        path="session/ABC123/28", snapshot=28
    )
    assert await server.read_event() is None


async def test_operational_ingestion_does_not_block_process_cleanup() -> None:
    server = observed_server(ServerConfig(shard="test"))
    await server._observe_operational("dst.server.saved", {"snapshot": 1})
    server.child = cast("asyncio.subprocess.Process", Mock(pid=42, returncode=6))

    await asyncio.wait_for(server.finish(), 1)
    assert server.closed
    assert [record.event_name for record in observations(server)] == [
        "dst.server.saved",
        "dst.server.process_exited",
    ]


@pytest.mark.parametrize("source", ["stdout", "lifecycle"])
async def test_input_pump_failure_survives_cleanup(source: str) -> None:
    server = observed_server(ServerConfig(shard="test"))
    server.child = cast("asyncio.subprocess.Process", Mock(pid=42, returncode=6))
    reader = asyncio.StreamReader()
    failure = OSError("injected input failure")
    reader.set_exception(failure)
    transport = Mock()
    server.read_transports = (transport,)
    if source == "stdout":
        server.log_task = asyncio.create_task(server.pump_logs(reader))
    else:
        server.lifecycle_task = asyncio.create_task(server._pump_lifecycle(reader))

    with pytest.raises(OSError, match="injected input failure") as raised:
        await server.finish()

    assert raised.value is failure
    assert server.closed
    transport.close.assert_called_once_with()
    assert await server.read_event() is None
    assert await server.read_game_event() is None
    records = observations(server)
    assert records[-1].event_name == "dst.server.process_exited"


async def test_command_result_eof_during_exit_does_not_fail_input_cleanup() -> None:
    server = observed_server(ServerConfig(shard="test"))
    server.child = cast("asyncio.subprocess.Process", Mock(pid=42, returncode=0))

    async def incomplete_command() -> str:
        await asyncio.sleep(0)
        message = "DST result stream closed before the command response completed"
        raise EOFError(message)

    console = Mock(
        reader_task=asyncio.create_task(incomplete_command()), close=AsyncMock()
    )
    server.console = console
    await server.finish()

    assert server.closed
    console.close.assert_awaited_once_with()
    assert [record.event_name for record in observations(server)] == [
        "dst.server.process_exited"
    ]


@pytest.mark.parametrize("operation", ["stop", "kill"])
async def test_requested_termination_is_not_reported_as_crash(operation: str) -> None:
    server = observed_server(ServerConfig(shard="test"))
    process = Mock(pid=42, returncode=None)

    def signalled() -> None:
        process.returncode = -9

    process.terminate.side_effect = signalled
    process.kill.side_effect = signalled
    process.wait = AsyncMock(return_value=-9)
    server.child = cast("asyncio.subprocess.Process", process)

    assert await getattr(server, operation)() == -9
    records = observations(server)
    assert records[-1].body == {"returncode": -9, "requested": True}
    assert records[-1].severity_text == "INFO"


async def test_process_creation_failure_closes_all_observation_readers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    server = observed_server(
        ServerConfig(shard="missing", persistent_storage_root=tmp_path)
    )
    monkeypatch.setattr(
        server_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("private executable failed")),
    )
    with pytest.raises(OSError, match="private executable"):
        await server.start()
    async with asyncio.timeout(1):
        assert await server.read_event() is None
        assert await server.read_game_event() is None
        records = observations(server)
    assert server.closed
    assert (
        "private executable"
        not in orjson.dumps([record.body for record in records]).decode()
    )


async def test_crash_before_ready_keeps_diagnostics_and_process_attempt(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "crashing-server"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os\n"
        "os.write(1, b'LUA ERROR stack traceback:')\n"
        "os._exit(6)\n"
    )
    executable.chmod(0o755)
    server = observed_server(
        ServerConfig(
            shard="test", executable=executable, persistent_storage_root=tmp_path
        ),
        log_handler=lambda _: None,
    )

    with pytest.raises(EOFError):
        await server.start(startup_timeout=1)
    records = observations(server)
    assert [record.event_name for record in records] == [
        "dst.server.process_started",
        "dst.runtime.diagnostic",
        "dst.server.process_exited",
    ]
    assert records[-1].body == {"returncode": 6, "requested": False}
    assert server.closed


async def test_stdout_rpc_responses_cannot_complete_pending_command() -> None:
    logs: list[str] = []
    server = observed_server(ServerConfig(shard="test"), log_handler=logs.append)
    writer = StubWriter()
    response = asyncio.StreamReader()
    console = Console(
        cast("asyncio.StreamWriter", writer),
        response,
        server.game_events.nonce,
        server.recorder,
    )
    server.console = console
    command = asyncio.create_task(console.execute("health", {}, 0))
    try:
        async with asyncio.timeout(1):
            request = await next_request(writer)
            stdout = asyncio.StreamReader()
            feed_response(stdout, request, True)
            stdout.feed_eof()
            await server.pump_logs(stdout)
            await asyncio.sleep(0)

            assert len(logs) == 3
            assert all(line.startswith("DST_RPC|") for line in logs[:2])
            assert logs[-1] == "DST_RemoteCommandDone"
            assert not command.done()
            assert not console.reader_task.done()
            assert not console.closed

            feed_response(response, request, True)
            assert orjson.loads(await command) == {"ok": True, "data": True}
            assert len(writer.commands) == 1
            assert not console.closed
    finally:
        command.cancel()
        await server.finish()
        await asyncio.gather(command, return_exceptions=True)
