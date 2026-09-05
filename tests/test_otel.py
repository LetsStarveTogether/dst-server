import asyncio
import importlib
import sqlite3
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import time_ns
from typing import Any
from unittest.mock import Mock

import grpc
import pytest
from google.rpc.error_details_pb2 import RetryInfo
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsPartialSuccess,
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2_grpc import (
    LogsServiceServicer,
    add_LogsServiceServicer_to_server,
)
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as ProtoLogRecord
from opentelemetry.sdk.resources import Resource

from dst_server.events import GAME_EVENT_ADAPTER, ObservedGameEvent
from dst_server.telemetry import otel

ATTEMPT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
INSTANCE = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


class ExportError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, retry_after: float | None = None) -> None:
        self.status = code
        self.retry_after = retry_after

    def code(self) -> grpc.StatusCode:
        return self.status

    def details(self) -> str:
        return "private payload and credential must never appear in status"

    def trailing_metadata(self) -> tuple[tuple[str, bytes], ...]:
        if self.retry_after is None:
            return ()
        info = RetryInfo()
        info.retry_delay.FromNanoseconds(round(self.retry_after * 1_000_000_000))
        return (("google.rpc.retryinfo-bin", info.SerializeToString()),)


class Sender(otel.OTLPLogSender):
    def __init__(self, *responses: ExportLogsServiceResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.requests: list[bytes] = []
        self.failure: grpc.StatusCode | None = None
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.closed = 0

    def export(self, payload: bytes) -> ExportLogsServiceResponse:
        self.requests.append(payload)
        self.entered.set()
        if not self.release.wait(3):
            message = "test exporter was never released"
            raise TimeoutError(message)
        if self.failure is not None:
            raise ExportError(self.failure)
        response = self.responses.popleft() if self.responses else None
        if isinstance(response, BaseException):
            raise response
        return response or ExportLogsServiceResponse()

    def shutdown(self) -> None:
        self.closed += 1
        self.release.set()


def observed(*, generation: int = 1, sequence: int = 1) -> ObservedGameEvent:
    event = GAME_EVENT_ADAPTER.validate_python({
        "v": 2,
        "nonce": ATTEMPT,
        "generation": generation,
        "session_id": "original-session",
        "seq": sequence,
        "event": "dst.world.state_changed",
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": {"name": "cycles", "value": 2},
    })
    return ObservedGameEvent(record=event, observed_timestamp_ns=time_ns())


def make_pipeline(
    tmp_path: Path, sender: Sender, *, max_bytes: int = 1_000_000
) -> otel.Pipeline:
    delivery = importlib.import_module("dst_server.telemetry.delivery")
    outbox = delivery.Outbox(tmp_path / "events.sqlite3", max_bytes=max_bytes)
    return otel.Pipeline(
        resource=Resource({
            "service.name": "dst-server",
            "service.instance.id": INSTANCE,
        }),
        outbox=outbox,
        sender=sender,
        _resources=(),
    )


def records(payload: bytes) -> list[ProtoLogRecord]:
    request = ExportLogsServiceRequest.FromString(payload)
    return [
        record
        for resource in request.resource_logs
        for scope in resource.scope_logs
        for record in scope.log_records
    ]


def attributes(record: ProtoLogRecord) -> dict[str, Any]:
    return {
        attribute.key: getattr(attribute.value, kind)
        for attribute in record.attributes
        if (kind := attribute.value.WhichOneof("value")) is not None
    }


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():  # ruff: ignore[async-busy-wait]
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def quick_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel, "RETRY_INITIAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(otel, "RETRY_MAX_SECONDS", 0.02, raising=False)


async def test_emit_event_commits_before_return_and_keeps_source_identity(
    tmp_path: Path,
) -> None:
    sender = Sender()
    sender.release.clear()
    pipeline = make_pipeline(tmp_path, sender)
    event = observed()
    try:
        await pipeline.emit_event(
            event,
            attributes={
                "dst.cluster.name": "dst-000",
                "dst.shard.name": "forest",
                "dst.session.id": "wrong-session",
                "dst.event.sequence": 999,
            },
        )
        assert pipeline.status().pending == 1
        await until(sender.entered.is_set)
        record = records(sender.requests[0])[0]
        values = attributes(record)
        assert record.event_name == "dst.world.state_changed"
        assert record.time_unix_nano == event.observed_timestamp_ns
        assert record.observed_time_unix_nano == event.observed_timestamp_ns
        assert record.severity_text == "INFO"
        assert values["log.record.uid"] == f"{ATTEMPT}:1:1"
        assert values["dst.game.attempt.id"] == ATTEMPT
        assert values["dst.runtime.generation"] == 1
        assert values["dst.event.sequence"] == 1
        assert values["dst.session.id"] == "original-session"
        assert record.body.kvlist_value.values[0].key == "name"
    finally:
        sender.release.set()
        await pipeline.shutdown()


async def test_same_sequence_in_new_generation_is_a_distinct_log(
    tmp_path: Path,
) -> None:
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed(generation=1))
        await pipeline.emit_event(observed(generation=2))
        await until(lambda: pipeline.status().pending == 0)
        uids = {
            attributes(record)["log.record.uid"]
            for payload in sender.requests
            for record in records(payload)
        }
        assert uids == {f"{ATTEMPT}:1:1", f"{ATTEMPT}:2:1"}
    finally:
        await pipeline.shutdown()


async def test_instrumentation_failure_has_error_severity(tmp_path: Path) -> None:
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender)
    source = observed()
    failure = GAME_EVENT_ADAPTER.validate_python(
        source.record.model_dump()
        | {
            "event": "dst.telemetry.error",
            "data": {
                "stage": "player.combat_hit",
                "message": "callback_failed",
                "count": 2,
            },
        }
    )
    try:
        await pipeline.emit_event(
            ObservedGameEvent(failure, source.observed_timestamp_ns)
        )
        await until(lambda: pipeline.status().pending == 0)
        record = records(sender.requests[0])[0]
        assert record.severity_text == "ERROR"
        assert record.severity_number == 17
    finally:
        await pipeline.shutdown()


async def test_operational_logs_preserve_severity_body_and_uid(tmp_path: Path) -> None:
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender)
    timestamp = time_ns()
    body = {"message": "Lua crashed", "exit_code": 6, "stacktrace": "a\nb"}
    try:
        await pipeline.emit_operational(
            event_name="dst.process.exited",
            body=body,
            observed_timestamp_ns=timestamp,
            severity_text="ERROR",
            attributes={"log.record.uid": "process-exit-1", "dst.shard.name": "forest"},
        )
        body["message"] = "mutated after commit"
        await until(lambda: pipeline.status().pending == 0)
        record = records(sender.requests[0])[0]
        assert record.event_name == "dst.process.exited"
        assert record.severity_text == "ERROR"
        assert record.severity_number == 17
        assert record.time_unix_nano == timestamp
        assert attributes(record)["log.record.uid"] == "process-exit-1"
        decoded = {item.key: item.value for item in record.body.kvlist_value.values}
        assert decoded["message"].string_value == "Lua crashed"
        assert decoded["exit_code"].int_value == 6
        assert decoded["stacktrace"].string_value == "a\nb"
    finally:
        await pipeline.shutdown()


async def test_operational_encoding_never_silently_truncates_attributes(
    tmp_path: Path,
) -> None:
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender)
    values = {f"field-{number}": str(number) for number in range(150)}
    try:
        await pipeline.emit_operational(
            event_name="dst.test.attributes",
            body={"nested": {"list": [None, True, 42, 1.5, "中文"]}},
            observed_timestamp_ns=time_ns(),
            severity_text="INFO",
            attributes=values,
        )
        await until(lambda: pipeline.status().pending == 0)
        record = records(sender.requests[0])[0]
        assert values.items() <= attributes(record).items()
        assert attributes(record)["log.record.uid"]
        assert record.dropped_attributes_count == 0
    finally:
        await pipeline.shutdown()


async def test_collector_outage_reopens_and_replays_original_wire_bytes(
    tmp_path: Path,
) -> None:
    offline = Sender()
    offline.failure = grpc.StatusCode.UNAVAILABLE
    pipeline = make_pipeline(tmp_path, offline)
    event = observed()
    await pipeline.emit_event(event)
    await until(lambda: bool(offline.requests))
    original = offline.requests[0]
    assert pipeline.status().pending == 1
    await pipeline.shutdown(timeout=0.1)

    recovered = Sender()
    replay = make_pipeline(tmp_path, recovered)
    try:
        await until(lambda: replay.status().pending == 0)
        assert recovered.requests == [original]
        record = records(recovered.requests[0])[0]
        assert record.time_unix_nano == event.observed_timestamp_ns
        assert attributes(record)["log.record.uid"] == f"{ATTEMPT}:1:1"
        resource = (
            ExportLogsServiceRequest.FromString(original).resource_logs[0].resource
        )
        assert any(
            item.key == "service.instance.id" and item.value.string_value == INSTANCE
            for item in resource.attributes
        )
    finally:
        await replay.shutdown()


async def test_network_outage_does_not_block_durable_append_or_event_loop(
    tmp_path: Path,
) -> None:
    sender = Sender()
    sender.release.clear()
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(sender.entered.is_set)
        async with asyncio.timeout(1):
            for sequence in range(2, 12):
                await pipeline.emit_event(observed(sequence=sequence))
        assert pipeline.status().pending == 11
        assert len(sender.requests) == 1
    finally:
        sender.release.set()
        await pipeline.shutdown()


async def test_disk_append_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender)
    entered, release = Event(), Event()
    assert pipeline.outbox is not None
    original = pipeline.outbox.append

    def slow_append(payload: bytes) -> int:
        entered.set()
        if not release.wait(2):
            message = "disk worker blocked the event loop"
            raise TimeoutError(message)
        return original(payload)

    monkeypatch.setattr(pipeline.outbox, "append", slow_append)
    task = asyncio.create_task(pipeline.emit_event(observed()))
    try:
        await until(entered.is_set)
        assert not task.done()
        assert pipeline.status().pending == 0
        release.set()
        await task
    finally:
        release.set()
        await pipeline.shutdown()


async def test_disk_capacity_failure_is_explicit_and_never_reports_success(
    tmp_path: Path,
) -> None:
    delivery = importlib.import_module("dst_server.telemetry.delivery")
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender, max_bytes=1)
    try:
        with pytest.raises(delivery.OutboxFull):
            await pipeline.emit_event(observed())
        assert pipeline.status().pending == 0
        assert sender.requests == []
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED]
)
async def test_transient_failure_retries_identical_wire_bytes(
    tmp_path: Path, code: grpc.StatusCode
) -> None:
    sender = Sender(ExportError(code), ExportLogsServiceResponse())
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(lambda: pipeline.status().pending == 0)
        assert len(sender.requests) == 2
        assert sender.requests[0] == sender.requests[1]
        assert pipeline.status().quarantined == 0
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize("retry_after", [None, 0.075])
async def test_resource_exhaustion_only_retries_with_receiver_retry_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_after: float | None
) -> None:
    sender = Sender(ExportError(grpc.StatusCode.RESOURCE_EXHAUSTED, retry_after))
    waits: list[float] = []

    async def wait_retry(_self: otel.Pipeline, delay: float) -> None:
        waits.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(otel.Pipeline, "_wait_retry", wait_retry)
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(lambda: pipeline.status().pending == 0)
        if retry_after is None:
            assert pipeline.status().quarantined == 1
            assert len(sender.requests) == 1
            assert waits == []
        else:
            assert pipeline.status().quarantined == 0
            assert len(sender.requests) == 2
            assert waits == [retry_after]
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize("valid_records", [0, 2])
async def test_malformed_stored_record_is_isolated_from_valid_siblings(
    tmp_path: Path, valid_records: int
) -> None:
    delivery = importlib.import_module("dst_server.telemetry.delivery")
    outbox = delivery.Outbox(tmp_path / "events.sqlite3")
    outbox.append(b"\xff")
    request = ExportLogsServiceRequest()
    request.resource_logs.add().scope_logs.add().log_records.add(event_name="dst.valid")
    for _ in range(valid_records):
        outbox.append(request.SerializeToString())
    sender = Sender()
    pipeline = otel.Pipeline(Resource({}), outbox, sender, ())
    try:
        await until(lambda: pipeline.status().pending == 0)
        assert pipeline.status().quarantined == 1
        assert (
            sum(len(records(payload)) for payload in sender.requests) == valid_records
        )
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.PERMISSION_DENIED]
)
async def test_permanent_failure_is_quarantined_and_next_event_proceeds(
    tmp_path: Path, code: grpc.StatusCode
) -> None:
    sender = Sender(ExportError(code))
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(lambda: pipeline.status().quarantined == 1)
        assert pipeline.status().pending == 0
        status = pipeline.status()
        assert status.last_error is not None
        assert "private payload" not in status.last_error
        await pipeline.emit_event(observed(sequence=2))
        await until(lambda: pipeline.status().pending == 0)
        assert len(sender.requests) == 2
        assert pipeline.status().quarantined == 1
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize("rejected", [0, 1])
async def test_partial_success_is_never_retried(tmp_path: Path, rejected: int) -> None:
    sender = Sender(
        ExportLogsServiceResponse(
            partial_success=ExportLogsPartialSuccess(
                rejected_log_records=rejected,
                error_message="private receiver detail",
            )
        )
    )
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(lambda: pipeline.status().pending == 0)
        assert len(sender.requests) == 1
        assert pipeline.status().quarantined == rejected
        assert "private receiver detail" not in (pipeline.status().last_error or "")
    finally:
        await pipeline.shutdown()


async def test_partial_rejection_quarantines_whole_batch_without_guessing_ids(
    tmp_path: Path,
) -> None:
    sender = Sender(
        ExportLogsServiceResponse(),
        ExportLogsServiceResponse(
            partial_success=ExportLogsPartialSuccess(
                rejected_log_records=1, error_message="one of two records rejected"
            )
        ),
    )
    sender.release.clear()
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(sender.entered.is_set)
        await pipeline.emit_event(observed(sequence=2))
        await pipeline.emit_event(observed(sequence=3))
        sender.release.set()
        await until(lambda: pipeline.status().pending == 0)
        assert pipeline.status().quarantined == 2
        assert len(sender.requests) == 2
        assert len(records(sender.requests[1])) == 2
    finally:
        sender.release.set()
        await pipeline.shutdown()


async def test_unknown_exporter_exception_keeps_data_and_exposes_failure(
    tmp_path: Path,
) -> None:
    sender = Sender(RuntimeError("private implementation failure"))
    pipeline = make_pipeline(tmp_path, sender)
    try:
        await pipeline.emit_event(observed())
        await until(lambda: pipeline.status().pending == 0)
        assert len(sender.requests) == 2
        assert sender.requests[0] == sender.requests[1]
        assert "private implementation" not in (pipeline.status().last_error or "")
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize(
    ("operation", "code"),
    [("acknowledge", None), ("quarantine", grpc.StatusCode.INVALID_ARGUMENT)],
)
async def test_delivery_storage_failure_retains_record_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    code: grpc.StatusCode | None,
) -> None:
    sender = Sender()
    sender.failure = code
    pipeline = make_pipeline(tmp_path, sender)
    recovered = False
    persist_result = getattr(pipeline.outbox, operation)

    def fail_ack(*args: Any, **kwargs: Any) -> None:
        if not recovered:
            message = "private database failure"
            raise sqlite3.OperationalError(message)
        persist_result(*args, **kwargs)

    monkeypatch.setattr(pipeline.outbox, operation, fail_ack)
    try:
        await pipeline.emit_event(observed())
        await until(lambda: pipeline.status().last_error is not None)
        assert pipeline.status().pending == 1
        status = pipeline.status()
        assert status.last_error is not None
        assert "private database" not in status.last_error
        recovered = True
        await until(lambda: pipeline.status().pending == 0)
        assert set(sender.requests) == {sender.requests[0]}
    finally:
        await pipeline.shutdown()


async def test_cancelled_append_is_settled_before_shutdown_closes_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender()
    sender.failure = grpc.StatusCode.UNAVAILABLE
    pipeline = make_pipeline(tmp_path, sender)
    entered, release = Event(), Event()
    assert pipeline.outbox is not None
    append = pipeline.outbox.append

    def blocked_append(payload: bytes) -> int:
        entered.set()
        if not release.wait(2):
            message = "append worker did not settle"
            raise TimeoutError(message)
        return append(payload)

    monkeypatch.setattr(pipeline.outbox, "append", blocked_append)
    pending = asyncio.create_task(pipeline.emit_event(observed()))
    await until(entered.is_set)
    pending.cancel()
    closing = asyncio.create_task(pipeline.shutdown(timeout=0.1))
    await asyncio.sleep(0)
    release.set()
    try:
        async with asyncio.timeout(1):
            results = await asyncio.gather(pending, closing, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        delivery = importlib.import_module("dst_server.telemetry.delivery")
        reopened = delivery.Outbox(tmp_path / "events.sqlite3")
        try:
            assert reopened.stats().pending == 1
        finally:
            reopened.close()
    finally:
        release.set()
        await pipeline.shutdown()


async def test_shutdown_rejects_new_events_but_finishes_admitted_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender()
    sender.failure = grpc.StatusCode.UNAVAILABLE
    pipeline = make_pipeline(tmp_path, sender)
    entered, release = Event(), Event()
    assert pipeline.outbox is not None
    append = pipeline.outbox.append

    def blocked_append(payload: bytes) -> int:
        entered.set()
        if not release.wait(2):
            message = "append worker did not settle"
            raise TimeoutError(message)
        return append(payload)

    monkeypatch.setattr(pipeline.outbox, "append", blocked_append)
    pending = asyncio.create_task(pipeline.emit_event(observed()))
    await until(entered.is_set)
    closing = asyncio.create_task(pipeline.shutdown(timeout=0.1))
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="closed"):
            await pipeline.emit_event(observed(sequence=2))
        release.set()
        async with asyncio.timeout(1):
            await pending
            await closing
    finally:
        release.set()
        await pipeline.shutdown()


async def test_shutdown_is_bounded_offline_and_idempotent(tmp_path: Path) -> None:
    sender = Sender()
    sender.failure = grpc.StatusCode.UNAVAILABLE
    pipeline = make_pipeline(tmp_path, sender)
    await pipeline.emit_event(observed())
    async with asyncio.timeout(1):
        await pipeline.shutdown(timeout=0.05)
        await pipeline.shutdown(timeout=0.05)
    assert sender.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        await pipeline.emit_event(observed(sequence=2))
    delivery = importlib.import_module("dst_server.telemetry.delivery")
    reopened = delivery.Outbox(tmp_path / "events.sqlite3")
    try:
        assert reopened.stats().pending == 1
    finally:
        reopened.close()


async def test_provider_cleanup_continues_after_one_shutdown_failure(
    tmp_path: Path,
) -> None:
    sender = Sender()
    pipeline = make_pipeline(tmp_path, sender)
    first, second = Mock(), Mock()
    first.shutdown.side_effect = RuntimeError("provider failure")
    pipeline._resources = (first, second)
    with pytest.raises(RuntimeError, match="provider failure"):
        await pipeline.shutdown()
    first.shutdown.assert_called_once_with()
    second.shutdown.assert_called_once_with()
    assert sender.closed == 1


def test_grpc_sender_preserves_partial_response_and_standard_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = []

    class Receiver(LogsServiceServicer):
        def Export(  # ruff: ignore[invalid-function-name]
            self, request: ExportLogsServiceRequest, context: grpc.ServicerContext
        ) -> ExportLogsServiceResponse:
            received.append((request, dict(context.invocation_metadata())))
            return ExportLogsServiceResponse(
                partial_success=ExportLogsPartialSuccess(
                    rejected_log_records=1, error_message="rejected"
                )
            )

    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    add_LogsServiceServicer_to_server(Receiver(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", f"http://127.0.0.1:{port}")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-test=value%20with%20spaces"
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "1")
    sender = None
    try:
        sender = otel.OTLPLogSender()
        response = sender.export(ExportLogsServiceRequest().SerializeToString())
        assert response.partial_success.rejected_log_records == 1
        assert received[0][1]["x-test"] == "value with spaces"
    finally:
        if sender is not None:
            sender.shutdown()
        server.stop(0).wait(1)


async def test_configure_logs_disabled_creates_no_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for signal in ("LOGS", "METRICS", "TRACES"):
        monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "none")
    pipeline = otel.configure(outbox_path=tmp_path / "events.sqlite3")
    try:
        assert not pipeline.logs_enabled
        assert not (tmp_path / "events.sqlite3").exists()
    finally:
        await pipeline.shutdown()


@pytest.mark.parametrize("signal", ["LOGS", "METRICS", "TRACES"])
def test_configure_rejects_unknown_signal_exporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "console")
    with pytest.raises(ValueError, match=f"OTEL_{signal}_EXPORTER"):
        otel.configure(outbox_path=tmp_path / "events.sqlite3")


def test_configure_rejects_non_ulid_instance_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"service\.instance\.id must be a ULID"):
        otel.configure(
            outbox_path=tmp_path / "events.sqlite3",
            resource_attributes={"service.instance.id": "not-an-instance-id"},
        )


def test_configure_installs_global_providers_only_once(tmp_path: Path) -> None:
    source = """
import asyncio
import os
import sys
from pathlib import Path
from opentelemetry import metrics, trace
from ulid import ULID
from dst_server.telemetry import otel
for signal in ("LOGS", "METRICS", "TRACES"):
    os.environ[f"OTEL_{signal}_EXPORTER"] = "none"
async def main():
    pipeline = otel.configure(outbox_path=Path(sys.argv[1]))
    assert trace.get_tracer_provider() in pipeline._resources
    assert metrics.get_meter_provider() in pipeline._resources
    identifier = trace.get_tracer_provider().resource.attributes["service.instance.id"]
    assert str(ULID.from_str(identifier)) == identifier
    try:
        otel.configure(outbox_path=Path(sys.argv[1]))
    except RuntimeError as error:
        assert "already" in str(error)
    else:
        raise AssertionError("global providers configured twice")
    await pipeline.shutdown()
asyncio.run(main())
"""
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", source, str(tmp_path / "events.sqlite3")],
        check=True,
        timeout=10,
    )


def test_configuration_failure_cleans_resources_and_allows_retry(
    tmp_path: Path,
) -> None:
    source = """
import asyncio
import os
import sys
import threading
from pathlib import Path
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from dst_server.telemetry import otel
os.environ["OTEL_LOGS_EXPORTER"] = "none"
os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
os.environ["OTEL_TRACES_EXPORTER"] = "otlp"
os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "60000"
otel.OTLPMetricExporter = ConsoleMetricExporter
otel.OTLPSpanExporter = lambda **_: InMemorySpanExporter()
original = otel.BatchSpanProcessor
failed = False
def fail_once(*args, **kwargs):
    global failed
    if not failed:
        failed = True
        raise RuntimeError("injected owner failure")
    return original(*args, **kwargs)
otel.BatchSpanProcessor = fail_once
def metric_threads():
    return [thread for thread in threading.enumerate()
            if thread.name == "OtelPeriodicExportingMetricReader" and thread.is_alive()]
async def main():
    try:
        otel.configure(outbox_path=Path(sys.argv[1]))
    except RuntimeError as error:
        assert str(error) == "injected owner failure"
    else:
        raise AssertionError("configuration unexpectedly succeeded")
    assert not metric_threads()
    pipeline = otel.configure(outbox_path=Path(sys.argv[1]))
    assert metric_threads()
    await pipeline.shutdown()
    assert not metric_threads()
asyncio.run(main())
"""
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", source, str(tmp_path / "events.sqlite3")],
        check=True,
        timeout=10,
    )


def test_recorder_preserves_trace_parent_errors_and_metric_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode

    from dst_server.telemetry.recorder import Recorder

    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter_provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    monkeypatch.setattr(
        "dst_server.telemetry.recorder.trace.get_tracer", tracer_provider.get_tracer
    )
    monkeypatch.setattr(
        "dst_server.telemetry.recorder.metrics.get_meter", meter_provider.get_meter
    )
    recorder = Recorder("dst-000", "forest")
    try:
        with tracer_provider.get_tracer("tests").start_as_current_span(
            "parent"
        ) as parent:
            with recorder.operation("save", "session"):
                recorder.record_event("accepted", event_name="dst.player.action")
                recorder.record_action("CHOP", True)
                recorder.set_process_up(True)
                recorder.set_player_count(1)
            message = "failed"
            with (
                pytest.raises(RuntimeError, match="failed"),
                recorder.operation("save", "session"),
            ):
                raise RuntimeError(message)
        spans = exporter.get_finished_spans()
        operations = [span for span in spans if span.name == "dst.server.save"]
        assert len(operations) == 2
        assert all(
            span.parent.span_id == parent.get_span_context().span_id
            for span in operations
        )
        assert operations[1].status.status_code is StatusCode.ERROR
        assert operations[1].attributes["error.type"] == "builtins.RuntimeError"
        assert operations[0].attributes["dst.session.id"] == "session"
        data = reader.get_metrics_data()
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "dst.player.action.count",
            "dst.server.operation.duration",
            "dst.server.player.count",
            "dst.telemetry.event.count",
        } <= names
    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()
