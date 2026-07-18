from __future__ import annotations

from time import time_ns

import pytest

pytest.importorskip("opentelemetry.sdk._logs")

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from dst_server import otel
from dst_server.events import GAME_EVENT_ADAPTER, GameEvent, ObservedGameEvent
from dst_server.otel import emit_game_event
from tests.helpers import StubServer, room_data, structured_result


def player_action(nonce: str) -> GameEvent:
    return GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 1,
            "nonce": nonce,
            "seq": 1,
            "event": "dst.player.action",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {
                "action_id": "CHOP",
                "action_sequence": 1,
                "success": True,
                "reason": None,
                "actor": {
                    "prefab": "wilson",
                    "guid": 42,
                    "userid": "KU_TEST",
                    "position": {"x": 1.0, "y": 0.0, "z": 2.0},
                },
                "target": None,
                "initial_target_owner": None,
                "inventory_object": None,
                "position": None,
                "recipe": None,
                "forced": False,
            },
        },
        strict=True,
    )


async def exercise_pipeline(server: StubServer, pipeline: otel.OtelPipeline) -> int:
    tracer = pipeline.tracer_provider.get_tracer("tests")
    with tracer.start_as_current_span("bot.operation") as parent:
        parent_span_id = parent.get_span_context().span_id
        await server.game.world.room()
    with pytest.raises(RuntimeError, match="boom"):
        await server.game.world.request_save()
    event = player_action(server.game_events.nonce)
    assert server.game_events.accept(
        "DST_OTEL|" + event.model_dump_json(),
        time_ns(),
    )
    observed = await server.read_game_event()
    assert observed is not None
    emit_game_event(pipeline.logger, observed, server=server)
    assert await pipeline.force_flush()
    return parent_span_id


def test_emit_game_event_as_structured_otel_event() -> None:
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = provider.get_logger("dst-server.game-events", "0.1.0")
    observed_timestamp_ns = time_ns()
    event = GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 1,
            "nonce": "0123456789abcdef",
            "seq": 1,
            "event": "dst.world.state_changed",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {"name": "cycles", "value": 2},
        },
        strict=True,
    )

    try:
        emit_game_event(
            logger,
            ObservedGameEvent(
                record=event,
                observed_timestamp_ns=observed_timestamp_ns,
            ),
        )

        (readable,) = exporter.get_finished_logs()
        record = readable.log_record
        assert record.event_name == "dst.world.state_changed"
        assert record.body == {"name": "cycles", "value": 2}
        assert record.timestamp == observed_timestamp_ns
        assert record.observed_timestamp == observed_timestamp_ns
        assert record.attributes == {
            "dst.event.sequence": 1,
            "dst.tick": 10,
            "dst.monotonic_ms": 20,
            "dst.world.cycle": 2,
        }
    finally:
        provider.shutdown()


async def test_otlp_pipeline_exports_traces_metrics_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    metric_reader = InMemoryMetricReader()
    monkeypatch.setattr(otel, "OTLPSpanExporter", lambda **_kwargs: span_exporter)
    monkeypatch.setattr(otel, "OTLPLogExporter", lambda **_kwargs: log_exporter)
    monkeypatch.setattr(otel, "OTLPMetricExporter", object)
    monkeypatch.setattr(
        otel,
        "PeriodicExportingMetricReader",
        lambda _exporter: metric_reader,
    )
    pipeline = otel.configure_otlp(
        service_instance_id="dst-test-instance",
        resource_attributes={"service.namespace": "tests"},
    )
    server = StubServer([
        structured_result(room_data()),
        'DST_SERVER_RESULT|{"ok":false,"error":"boom"}',
    ])
    server.server_events.session_id = "TEST"

    try:
        parent_span_id = await exercise_pipeline(server, pipeline)

        spans = {span.name: span for span in span_exporter.get_finished_spans()}
        span = spans["dst.server.lua.get_room"]
        assert span.name == "dst.server.lua.get_room"
        assert span.parent is not None
        assert span.parent.span_id == parent_span_id
        assert span.attributes is not None
        assert span.attributes["dst.lua.method"] == "get_room"
        assert span.attributes["dst.session.id"] == "TEST"
        assert span.resource.attributes["service.instance.id"] == "dst-test-instance"
        failed_span = spans["dst.server.lua.save"]
        assert failed_span.status.status_code is StatusCode.ERROR
        assert failed_span.attributes is not None
        assert failed_span.attributes["error.type"] == "builtins.RuntimeError"

        metric_data = metric_reader.get_metrics_data()
        assert metric_data is not None
        exported_metrics = {
            metric.name: metric
            for resource in metric_data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "dst.player.action.count",
            "dst.server.operation.duration",
            "dst.server.player.count",
            "dst.telemetry.event.count",
            "dst.telemetry.queue.size",
        } <= exported_metrics.keys()
        action_points = exported_metrics["dst.player.action.count"].data.data_points
        assert any(
            point.attributes is not None
            and point.attributes["dst.action.name"] == "CHOP"
            and point.attributes["dst.action.success"] is True
            for point in action_points
        )
        operation_points = exported_metrics[
            "dst.server.operation.duration"
        ].data.data_points
        assert any(
            point.attributes is not None
            and point.attributes.get("error.type") == "builtins.RuntimeError"
            for point in operation_points
        )

        (readable,) = log_exporter.get_finished_logs()
        assert readable.log_record.event_name == "dst.player.action"
        assert readable.log_record.attributes is not None
        assert readable.log_record.attributes["dst.session.id"] == "TEST"
        assert readable.resource.attributes["service.namespace"] == "tests"
    finally:
        await pipeline.shutdown()
