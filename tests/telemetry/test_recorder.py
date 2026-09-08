import pytest


def test_recorder_preserves_trace_parent_errors_and_metric_outcomes() -> None:
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
    recorder = Recorder(
        "dst-000",
        "forest",
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
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
            span.parent is not None
            and span.parent.span_id == parent.get_span_context().span_id
            for span in operations
        )
        assert operations[1].status.status_code is StatusCode.ERROR
        assert operations[1].attributes is not None
        assert operations[1].attributes["error.type"] == "builtins.RuntimeError"
        assert operations[0].attributes is not None
        assert operations[0].attributes["dst.session.id"] == "session"
        data = reader.get_metrics_data()
        assert data is not None
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
