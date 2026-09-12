from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import cache
from time import perf_counter
from typing import TYPE_CHECKING

import orjson
from logbook import Logger
from opentelemetry import metrics, trace
from opentelemetry.trace import Span
from opentelemetry.util.types import AttributeValue
from pydantic import JsonValue
from ulid import ULID

if TYPE_CHECKING:
    from dst_server.events import ObservedGameEvent

    from .otel import Pipeline

logger = Logger(__name__)


@dataclass(slots=True)
class _Diagnostic:
    first_ns: int
    last_ns: int
    body: dict[str, JsonValue]
    attributes: dict[str, AttributeValue]
    count: int = 0
    occurrences: int = 0
    reported: int = 0


type _Instruments = tuple[
    metrics.Histogram,
    metrics.UpDownCounter,
    metrics.UpDownCounter,
    metrics.UpDownCounter,
    metrics.Counter,
    metrics.Counter,
]


def _instruments(meter: metrics.Meter) -> _Instruments:
    return (
        meter.create_histogram(
            "dst.server.operation.duration",
            unit="s",
            description="Duration of DST server SDK operations.",
        ),
        meter.create_up_down_counter(
            "dst.server.process.count",
            unit="{process}",
            description="Managed DST server processes currently running.",
        ),
        meter.create_up_down_counter(
            "dst.server.player.count",
            unit="{player}",
            description="Players currently attached to this DST shard.",
        ),
        meter.create_up_down_counter(
            "dst.server.client.count",
            unit="{client}",
            description="Authenticated clients reported by this DST server.",
        ),
        meter.create_counter(
            "dst.telemetry.event.count",
            unit="{event}",
            description="DST game telemetry events by processing outcome.",
        ),
        meter.create_counter(
            "dst.player.action.count",
            unit="{action}",
            description="Completed player actions by action name and outcome.",
        ),
    )


@cache
def _default_instruments() -> _Instruments:
    # The default proxy provider retains every registered instrument until a
    # real provider is installed, so reuse one set across server attempts.
    return _instruments(metrics.get_meter("dst-server"))


class Recorder:
    def __init__(
        self,
        cluster: str,
        shard: str,
        *,
        tracer_provider: trace.TracerProvider | None = None,
        meter_provider: metrics.MeterProvider | None = None,
        pipeline: Pipeline | None = None,
    ) -> None:
        self.base_attributes = {
            "dst.cluster.name": cluster,
            "dst.shard.name": shard,
        }
        self.pipeline = pipeline
        self._diagnostics: dict[tuple[str, str], _Diagnostic] = {}
        self._sink_failures: dict[str, int] = {}
        self.player_count = 0
        self.client_count = 0
        self.process_up = False
        self.tracer = trace.get_tracer("dst-server", tracer_provider=tracer_provider)
        (
            self.operation_duration,
            self.process_count,
            self.player_count_metric,
            self.client_count_metric,
            self.telemetry_event_count,
            self.player_action_count,
        ) = (
            _default_instruments()
            if meter_provider is None
            else _instruments(
                metrics.get_meter("dst-server", meter_provider=meter_provider)
            )
        )

    def attributes(self, session_id: str | None = None) -> dict[str, str]:
        attributes = self.base_attributes.copy()
        if session_id is not None:
            attributes["dst.session.id"] = session_id
        return attributes

    def observe_log(
        self,
        *,
        event_name: str,
        body: Mapping[str, JsonValue],
        observed_timestamp_ns: int,
        severity_text: str = "INFO",
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> None:
        values: dict[str, AttributeValue] = self.base_attributes | dict(
            attributes or {}
        )
        values.setdefault("log.record.uid", str(ULID()))
        # Retain the same identity and source time before any bounded subscriber
        # or SDK exporter queue can discard this observation.
        try:
            record = {
                "event_name": event_name,
                "body": dict(body),
                "observed_timestamp_ns": observed_timestamp_ns,
                "severity_text": severity_text,
                "attributes": values,
                "resource": dict(self.pipeline.resource.attributes)
                if self.pipeline is not None
                else {},
            }
            logger.info(
                "{shard}: DST_RECORD|{record}",
                shard=self.base_attributes["dst.shard.name"],
                record=orjson.dumps(record).decode(),
            )
        except Exception:
            self._sink_failed("local_record_failed")
        if self.pipeline is not None and self.pipeline.logs_enabled:
            try:
                self.pipeline.emit_operational(
                    event_name=event_name,
                    body=body,
                    observed_timestamp_ns=observed_timestamp_ns,
                    severity_text=severity_text,
                    attributes=values,
                )
            except Exception:
                self._sink_failed("log_submission_failed")

    def _sink_failed(self, reason: str) -> None:
        # Never report sink failures through the failed sink itself, or let a
        # broken local log handler interrupt game state and control observation.
        total = self._sink_failures[reason] = self._sink_failures.get(reason, 0) + 1
        self.record_event("dropped", reason=reason)
        if total & (total - 1) == 0:
            with suppress(Exception):
                logger.warning(
                    "{shard}: telemetry sink failure: reason={reason} total={total}",
                    shard=self.base_attributes["dst.shard.name"],
                    reason=reason,
                    total=total,
                )

    def observe_game(self, observed: ObservedGameEvent) -> None:
        event = observed.record
        attributes: dict[str, AttributeValue] = {
            "log.record.uid": f"{event.nonce}:{event.generation}:{event.seq}",
            "dst.game.attempt.id": event.nonce,
            "dst.runtime.generation": event.generation,
            "dst.event.sequence": event.seq,
            "dst.tick": event.tick,
            "dst.monotonic_ms": event.monotonic_ms,
        }
        if event.session_id is not None:
            attributes["dst.session.id"] = event.session_id
        if event.cycle is not None:
            attributes["dst.world.cycle"] = event.cycle
        self.observe_log(
            event_name=event.event,
            body=event.data.model_dump(mode="json"),
            observed_timestamp_ns=observed.observed_timestamp_ns,
            severity_text="ERROR" if event.event == "dst.telemetry.error" else "INFO",
            attributes=attributes,
        )

    def diagnostic(
        self,
        kind: str,
        reason: str,
        observed_timestamp_ns: int,
        *,
        body: Mapping[str, JsonValue] | None = None,
        attributes: Mapping[str, AttributeValue] | None = None,
        count: int = 1,
    ) -> None:
        key = kind, reason
        state = self._diagnostics.get(key)
        if state is None:
            state = self._diagnostics[key] = _Diagnostic(
                observed_timestamp_ns, observed_timestamp_ns, {}, {}
            )
        state.last_ns = observed_timestamp_ns
        state.body = dict(body or {})
        state.attributes = dict(attributes or {})
        state.count += count
        state.occurrences += 1
        if state.occurrences & (state.occurrences - 1) == 0:
            self._report_diagnostic(key, state)

    def _report_diagnostic(self, key: tuple[str, str], state: _Diagnostic) -> None:
        kind, reason = key
        body: dict[str, JsonValue] = state.body | {
            "reason": reason,
            "count": state.count,
            "since_previous": state.count - state.reported,
            "occurrences": state.occurrences,
            "first_observed_timestamp_ns": state.first_ns,
            "last_observed_timestamp_ns": state.last_ns,
        }
        state.reported = state.count
        self.observe_log(
            event_name=f"dst.telemetry.{kind}",
            body=body,
            observed_timestamp_ns=state.last_ns,
            severity_text="WARN",
            attributes=state.attributes,
        )

    def flush_diagnostics(self) -> None:
        for key, state in self._diagnostics.items():
            if state.reported != state.count:
                self._report_diagnostic(key, state)

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

    def set_client_count(self, value: int) -> None:
        value = max(0, value)
        change = value - self.client_count
        self.client_count = value
        if change:
            self.client_count_metric.add(change, self.base_attributes)

    def record_event(
        self,
        outcome: str,
        *,
        event_name: str | None = None,
        reason: str | None = None,
        count: int = 1,
    ) -> None:
        attributes = self.base_attributes | {"dst.telemetry.outcome": outcome}
        if event_name is not None:
            attributes["dst.event.name"] = event_name
        if reason is not None:
            attributes["dst.telemetry.reason"] = reason
        self.telemetry_event_count.add(count, attributes)

    def record_action(self, name: str, success: bool) -> None:
        self.player_action_count.add(
            1,
            self.base_attributes
            | {"dst.action.name": name, "dst.action.success": success},
        )
