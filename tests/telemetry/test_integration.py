import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.sdk._logs.export import LogRecordExportResult
from ulid import ULID

from dst_server.cluster import agent as agent_module
from dst_server.cluster import cli, daemon, service
from dst_server.cluster.agent import ShardAgent
from dst_server.configuration.files import Shard
from dst_server.events import GAME_EVENT_ADAPTER, ObservedGameEvent
from dst_server.models.cluster import ShardPhase
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import otel


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ShardAgent:
    config = ServerConfig(shard="forest", executable=tmp_path / "unused")
    monkeypatch.setattr(
        agent_module.service, "create_server_config", Mock(return_value=config)
    )
    return ShardAgent(
        Shard("forest", True, tmp_path / "console"),
        install_path=tmp_path,
        cluster_path=tmp_path,
    )


def observation(attempt: str, *, generation: int = 1) -> ObservedGameEvent:
    return ObservedGameEvent(
        record=GAME_EVENT_ADAPTER.validate_python(
            {
                "v": 2,
                "nonce": attempt,
                "generation": generation,
                "session_id": "ORIGINAL_SESSION",
                "seq": 1,
                "event": "dst.world.state_changed",
                "tick": 10,
                "monotonic_ms": 20,
                "cycle": 3,
                "data": {"name": "cycles", "value": 3},
            },
            strict=True,
        ),
        observed_timestamp_ns=1_788_657_000_123_456_789,
    )


def event_server(relay: ShardAgent, *events: ObservedGameEvent) -> Server:
    return cast(
        "Server",
        SimpleNamespace(
            config=relay.config,
            lifecycle=SimpleNamespace(eof=False),
            game_events=SimpleNamespace(nonce=events[0].record.nonce),
            recorder=SimpleNamespace(
                attributes=Mock(return_value={"dst.shard.name": "forest"})
            ),
            driver=SimpleNamespace(observe_event=Mock()),
            session_id="LATER_SESSION",
            read_game_event=AsyncMock(side_effect=(*events, None)),
            returncode=None,
        ),
    )


async def test_game_relay_keeps_broadcasting_while_export_is_unavailable(
    relay: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = observation(str(ULID()))
    entered = Event()
    release = Event()

    def export(_records: object) -> LogRecordExportResult:
        entered.set()
        assert release.wait(3)
        return LogRecordExportResult.FAILURE

    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setenv("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "1")
    monkeypatch.setattr(otel, "OTLPLogExporter", Mock(return_value=Mock(export=export)))
    pipeline = relay._pipeline = otel.configure()
    subscription = relay.game_events.subscribe()
    try:
        await relay._drain_game_events(event_server(relay, observed))
        assert await asyncio.to_thread(entered.wait, 1)
        later = observation(observed.record.nonce, generation=2)
        async with asyncio.timeout(1):
            await relay._drain_game_events(event_server(relay, later))
            records = await subscription.next(2)
        assert [record.event for record in records] == [observed.record, later.record]
        assert records[0].observed_timestamp_ns == observed.observed_timestamp_ns
        assert records[0].event.session_id == "ORIGINAL_SESSION"
        assert not (relay.cluster_path / relay.name / ".telemetry.sqlite3").exists()
    finally:
        release.set()
        subscription.close()
        await pipeline.shutdown()


async def test_local_mode_preserves_each_generation_and_does_not_need_otel(
    relay: ShardAgent,
) -> None:
    attempt = str(ULID())
    events = (observation(attempt), observation(attempt, generation=2))
    subscription = relay.game_events.subscribe()

    await relay._drain_game_events(event_server(relay, *events))

    records = await subscription.next(2)
    assert [record.sequence for record in records] == [1, 2]
    assert [record.event.generation for record in records] == [1, 2]


async def test_operational_relay_uses_source_identity_time_and_severity(
    relay: ShardAgent,
) -> None:
    record = SimpleNamespace(
        uid=str(ULID()),
        event_name="dst.runtime.lua_error",
        body={"source": "workshop-123/scripts/example.lua", "line": 42},
        observed_timestamp_ns=1_788_657_000_123_456_789,
        severity_text="ERROR",
    )
    server = cast(
        "Server",
        SimpleNamespace(
            config=relay.config,
            game_events=SimpleNamespace(nonce=str(ULID())),
            read_operational_event=AsyncMock(side_effect=(record, None)),
            recorder=SimpleNamespace(attributes=Mock(return_value={})),
        ),
    )
    pipeline = SimpleNamespace(logs_enabled=True, emit_operational=Mock())
    relay._pipeline = cast("otel.Pipeline", pipeline)

    await relay._drain_operational(server)

    pipeline.emit_operational.assert_called_once()
    kwargs = pipeline.emit_operational.call_args.kwargs
    assert kwargs["event_name"] == record.event_name
    assert kwargs["body"] == record.body
    assert kwargs["observed_timestamp_ns"] == record.observed_timestamp_ns
    assert kwargs["severity_text"] == "ERROR"
    assert kwargs["attributes"]["log.record.uid"] == record.uid
    assert kwargs["attributes"]["dst.game.attempt.id"] == server.game_events.nonce


async def test_telemetry_relays_start_before_process_readiness_and_are_critical(
    relay: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = event_server(relay, observation(str(ULID())))
    failures = [OSError("event stream failed"), OSError("event stream failed")]
    monkeypatch.setattr(agent_module, "Server", Mock(return_value=server))
    monkeypatch.setattr(agent_module.console, "forward", AsyncMock())
    monkeypatch.setattr(relay, "_drain_lifecycle", AsyncMock())
    monkeypatch.setattr(relay, "_drain_game_events", AsyncMock(side_effect=failures[0]))
    monkeypatch.setattr(relay, "_drain_operational", AsyncMock(side_effect=failures[1]))
    calls: list[tuple[str, bool]] = []

    def done(
        _: Server, task: asyncio.Task[None], *, critical: bool, expected_eof: bool
    ) -> None:
        del expected_eof
        task.exception()
        calls.append((task.get_name(), critical))

    monkeypatch.setattr(relay, "_background_done", done)
    assert relay._new_server() is server
    assert len(relay._attempt_tasks) == 3
    await asyncio.gather(*relay._attempt_tasks, return_exceptions=True)
    await asyncio.sleep(0)

    assert ("dst-lifecycle-relay-forest", True) in calls
    assert ("dst-game-event-relay-forest", True) in calls
    assert ("dst-operational-relay-forest", True) in calls


async def test_finished_process_does_not_hide_event_stream_failure(
    relay: ShardAgent,
) -> None:
    server = event_server(relay, observation(str(ULID())))
    cast("SimpleNamespace", server).returncode = 6
    relay.supervisor = cast(
        "agent_module.ShardSupervisor",
        SimpleNamespace(
            server=server, status=SimpleNamespace(phase=ShardPhase.STOPPED)
        ),
    )

    async def fail() -> None:
        await asyncio.sleep(0)
        message = "unread process tail"
        raise OSError(message)

    task = asyncio.create_task(fail(), name="dst-operational-relay-forest")
    await asyncio.gather(task, return_exceptions=True)
    relay._background_done(server, task, critical=True)

    async with asyncio.timeout(1):
        with pytest.raises(RuntimeError, match="background task failed"):
            await relay.wait_fatal()


async def test_stopped_waits_for_both_telemetry_tails(relay: ShardAgent) -> None:
    server = event_server(relay, observation(str(ULID())))
    release = asyncio.Event()
    completed: list[str] = []

    async def tail(name: str) -> None:
        await release.wait()
        completed.append(name)

    fifo = asyncio.create_task(tail("fifo"))
    tails = tuple(asyncio.create_task(tail(name)) for name in ("game", "runtime"))
    relay._fifo_task = fifo
    relay._attempt_tasks = tails
    stop = asyncio.create_task(relay._stopped(server))
    try:
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        await stop
        assert completed == ["game", "runtime"]
        assert fifo.cancelled()
    finally:
        release.set()
        stop.cancel()
        await asyncio.gather(stop, fifo, *tails, return_exceptions=True)


@pytest.mark.parametrize("pipeline_mode", ["none", "logs_disabled", "logs_enabled"])
@pytest.mark.parametrize(
    "kind",
    [
        "ordinary",
        "diagnostic",
        "event",
        "invalid_payload",
        "invalid_prefix",
        "unicode_name",
        "invalid_utf8",
    ],
)
def test_child_log_routing_reaches_the_actual_cli_stdout(
    relay: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pipeline_mode: str,
    kind: str,
) -> None:
    enabled = pipeline_mode == "logs_enabled"
    pipeline = SimpleNamespace(
        logs_enabled=enabled,
        emit_event=Mock(),
        emit_operational=Mock(),
    )
    if pipeline_mode != "none":
        relay._pipeline = cast("otel.Pipeline", pipeline)
    server = Server(relay.config)
    server.log_handler = lambda line: relay._log(server, line)
    subscription = relay.logs.subscribe()
    rpc_lines: list[str] = []
    event = observation(server.game_events.nonce).record
    payload = event.model_dump_json()
    line = {
        "ordinary": "[00:00:01]: ordinary game output",
        "diagnostic": "[00:00:01]: LUA ERROR stack traceback:",
        "event": f"[00:00:01]: DST_OTEL|{payload}",
        "invalid_payload": "[00:00:01]: DST_OTEL|{invalid-json",
        "invalid_prefix": f"[00:00:01]: source.lua: DST_OTEL|{payload}",
        "unicode_name": (
            "[00:00:01]: player=\U000f0001\U000f001c👩\u200d💻❤️\u200b"
            "\u0085\u2028\u2029e\u0301"
        ),
        "invalid_utf8": (
            "[00:00:01]: native error: \ufffd\x00\ufffd\nafter invalid UTF-8"
        ),
    }[kind]
    raw_line = (
        b"[00:00:01]: native error: \xff\x00\xfe\nafter invalid UTF-8"
        if kind == "invalid_utf8"
        else line.encode()
    )

    async def master(**_arguments: object) -> int:
        reader = asyncio.StreamReader()
        reader.feed_data(raw_line + b"\n")
        reader.feed_eof()
        try:
            await server.pump_logs(reader)
        finally:
            await server.finish()
        await relay._drain_game_events(server)
        await relay._drain_operational(server)
        for _ in range(relay._log_sequence):
            rpc_lines.extend(record.line for record in await subscription.next(1))
        subscription.close()
        return 0

    monkeypatch.setattr(daemon, "master", master)
    monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", "off")
    assert cli.main(("master",)) == 0

    ordinary = kind in {
        "ordinary",
        "diagnostic",
        "invalid_prefix",
        "unicode_name",
        "invalid_utf8",
    }
    expected = [f"forest: {value}" for value in line.split("\n")] if ordinary else []
    if kind == "event" and not enabled:
        expected.append(f"forest: DST_EVENT|{payload}")
    if kind == "diagnostic" and not enabled:
        expected.append("forest: dst.runtime.diagnostic: {'kind': 'lua_error'}")
    if kind == "invalid_payload":
        expected.append("discard invalid DST game event")
    captured = capsys.readouterr()
    assert captured.out == "".join(value + "\n" for value in expected)
    assert captured.err == ""
    assert rpc_lines == (line.split("\n") if ordinary else [])
    assert relay._log_sequence == len(rpc_lines)
    assert relay._game_sequence == int(kind == "event")
    assert server.telemetry_invalid == int(kind == "invalid_payload")
    assert pipeline.emit_event.call_count == int(enabled and kind == "event")
    assert pipeline.emit_operational.call_count == int(enabled and kind == "diagnostic")
    if enabled and kind == "event":
        assert pipeline.emit_event.call_args.args[0].record == event
    if enabled and kind == "diagnostic":
        assert pipeline.emit_operational.call_args.kwargs["body"] == {
            "kind": "lua_error"
        }


def test_otel_preserves_resource_identity_without_storage_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    configure = Mock()
    monkeypatch.setattr(otel, "configure", configure)
    incarnation = str(ULID())
    for shard in ("forest", "cave"):
        config = ServerConfig(
            shard=shard,
            persistent_storage_root=tmp_path,
            conf_dir=".",
            cluster="042",
            telemetry_cluster="dst-042",
        )
        service.configure_otel(config, instance_id=incarnation)
        configure.assert_called_with(
            resource_attributes={
                "dst.cluster.name": "dst-042",
                "service.instance.id": incarnation,
            }
        )


def test_requested_otel_configuration_failure_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setattr(
        otel, "configure", Mock(side_effect=ValueError("invalid exporter"))
    )

    with pytest.raises(ValueError, match="invalid exporter"):
        service.configure_otel(ServerConfig(shard="forest"))


@pytest.mark.parametrize("disabled", [False, True])
def test_local_mode_does_not_configure_otel(
    monkeypatch: pytest.MonkeyPatch,
    disabled: bool,
) -> None:
    for variable in service.OTEL_ENDPOINTS:
        monkeypatch.delenv(variable, raising=False)
    if disabled:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    configure = Mock(side_effect=AssertionError("local mode opened exporter"))
    monkeypatch.setattr(otel, "configure", configure)

    assert service.configure_otel(ServerConfig(shard="forest")) is None
    configure.assert_not_called()
