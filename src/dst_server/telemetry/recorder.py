from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from opentelemetry import metrics, trace
from opentelemetry.trace import Span

TRACER = trace.get_tracer("dst-server")
METER = metrics.get_meter("dst-server")
OPERATION_DURATION = METER.create_histogram(
    "dst.server.operation.duration",
    unit="s",
    description="Duration of DST server SDK operations.",
)
PROCESS_COUNT = METER.create_up_down_counter(
    "dst.server.process.count",
    unit="{process}",
    description="Managed DST server processes currently running.",
)
PLAYER_COUNT = METER.create_up_down_counter(
    "dst.server.player.count",
    unit="{player}",
    description="Players currently attached to this DST shard.",
)
TELEMETRY_EVENT_COUNT = METER.create_counter(
    "dst.telemetry.event.count",
    unit="{event}",
    description="DST game telemetry events by processing outcome.",
)
PLAYER_ACTION_COUNT = METER.create_counter(
    "dst.player.action.count",
    unit="{action}",
    description="Completed player actions by action name and outcome.",
)
TELEMETRY_QUEUE_SIZE = METER.create_up_down_counter(
    "dst.telemetry.queue.size",
    unit="{event}",
    description="Validated DST game events waiting for the application.",
)


class Recorder:
    def __init__(self, cluster: str, shard: str) -> None:
        self.base_attributes = {
            "dst.cluster.name": cluster,
            "dst.shard.name": shard,
        }
        self.player_count = 0
        self.process_up = False

    def attributes(self, session_id: str | None = None) -> dict[str, str]:
        attributes = self.base_attributes.copy()
        if session_id is not None:
            attributes["dst.session.id"] = session_id
        return attributes

    @contextmanager
    def operation(self, name: str, session_id: str | None = None) -> Iterator[Span]:
        metric_attributes = self.base_attributes | {"dst.operation.name": name}
        started = perf_counter()
        with TRACER.start_as_current_span(
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
                OPERATION_DURATION.record(
                    perf_counter() - started,
                    metric_attributes,
                )

    def set_player_count(self, value: int) -> None:
        value = max(0, value)
        change = value - self.player_count
        self.player_count = value
        if change:
            PLAYER_COUNT.add(change, self.base_attributes)

    def set_process_up(self, value: bool) -> None:
        if self.process_up == value:
            return
        self.process_up = value
        PROCESS_COUNT.add(1 if value else -1, self.base_attributes)

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
        TELEMETRY_EVENT_COUNT.add(1, attributes)

    def record_action(self, name: str, success: bool) -> None:
        PLAYER_ACTION_COUNT.add(
            1,
            self.base_attributes
            | {"dst.action.name": name, "dst.action.success": success},
        )

    def change_queue_size(self, value: int) -> None:
        TELEMETRY_QUEUE_SIZE.add(value, self.base_attributes)


__all__ = ["Recorder"]
