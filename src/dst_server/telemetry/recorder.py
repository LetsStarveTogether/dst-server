from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from opentelemetry import metrics, trace
from opentelemetry.trace import Span


class Recorder:
    def __init__(self, cluster: str, shard: str) -> None:
        self.base_attributes = {
            "dst.cluster.name": cluster,
            "dst.shard.name": shard,
        }
        self.player_count = 0
        self.process_up = False
        self.tracer = trace.get_tracer("dst-server")
        meter = metrics.get_meter("dst-server")
        self.operation_duration = meter.create_histogram(
            "dst.server.operation.duration",
            unit="s",
            description="Duration of DST server SDK operations.",
        )
        self.process_count = meter.create_up_down_counter(
            "dst.server.process.count",
            unit="{process}",
            description="Managed DST server processes currently running.",
        )
        self.player_count_metric = meter.create_up_down_counter(
            "dst.server.player.count",
            unit="{player}",
            description="Players currently attached to this DST shard.",
        )
        self.telemetry_event_count = meter.create_counter(
            "dst.telemetry.event.count",
            unit="{event}",
            description="DST game telemetry events by processing outcome.",
        )
        self.player_action_count = meter.create_counter(
            "dst.player.action.count",
            unit="{action}",
            description="Completed player actions by action name and outcome.",
        )

    def attributes(self, session_id: str | None = None) -> dict[str, str]:
        attributes = self.base_attributes.copy()
        if session_id is not None:
            attributes["dst.session.id"] = session_id
        return attributes

    @contextmanager
    def operation(self, name: str, session_id: str | None = None) -> Iterator[Span]:
        metric_attributes = self.base_attributes | {"dst.operation.name": name}
        started = perf_counter()
        with self.tracer.start_as_current_span(
            f"dst.server.{name}",
            attributes=self.attributes(session_id) | {"dst.operation.name": name},
        ) as span:
            try:
                yield span
            except BaseException as error:
                error_type = f"{type(error).__module__}.{type(error).__qualname__}"
                span.set_attribute("error.type", error_type)
                metric_attributes["error.type"] = error_type
                raise
            finally:
                self.operation_duration.record(
                    perf_counter() - started,
                    metric_attributes,
                )

    def set_player_count(self, value: int) -> None:
        value = max(0, value)
        change = value - self.player_count
        self.player_count = value
        if change:
            self.player_count_metric.add(change, self.base_attributes)

    def set_process_up(self, value: bool) -> None:
        if self.process_up == value:
            return
        self.process_up = value
        self.process_count.add(1 if value else -1, self.base_attributes)

    def record_event(
        self,
        outcome: str,
        *,
        event_name: str | None = None,
        reason: str | None = None,
    ) -> None:
        attributes = self.base_attributes | {"dst.telemetry.outcome": outcome}
        if event_name is not None:
            attributes["dst.event.name"] = event_name
        if reason is not None:
            attributes["dst.telemetry.reason"] = reason
        self.telemetry_event_count.add(1, attributes)

    def record_action(self, name: str, success: bool) -> None:
        self.player_action_count.add(
            1,
            self.base_attributes
            | {"dst.action.name": name, "dst.action.success": success},
        )


__all__ = ["Recorder"]
