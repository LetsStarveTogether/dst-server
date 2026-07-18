from __future__ import annotations

import asyncio
import secrets

from logbook import Logger
from pydantic import ValidationError

from .events import (
    GAME_EVENT_ADAPTER,
    ObservedGameEvent,
    PlayerActionEvent,
    PlayerShardEnteredEvent,
    PlayerShardLeftEvent,
)
from .instrumentation import Instrumentation

QUEUE_SIZE = 1024
MAX_LINE_BYTES = 64 * 1024
PREFIX = "DST_OTEL|"
logger = Logger(__name__)


class GameEventStream:
    def __init__(self, instrumentation: Instrumentation) -> None:
        self.instrumentation = instrumentation
        self.nonce = secrets.token_urlsafe(24)
        self.queue: asyncio.Queue[ObservedGameEvent | None] = asyncio.Queue(
            maxsize=QUEUE_SIZE
        )
        self.eof = False
        self.invalid = 0
        self.dropped = 0

    async def read(self) -> ObservedGameEvent | None:
        if self.eof and self.queue.empty():
            return None
        event = await self.queue.get()
        if event is not None:
            self.instrumentation.change_queue_size(-1)
        return event

    def accept(self, line: str, observed_timestamp_ns: int) -> bool:
        marker = line.find(PREFIX)
        if marker < 0:
            return False

        payload = line[marker + len(PREFIX) :]
        if len(payload.encode()) > MAX_LINE_BYTES:
            self.reject("oversized", "discard oversized DST game event")
            return True
        try:
            event = GAME_EVENT_ADAPTER.validate_json(payload, strict=True)
        except ValidationError:
            self.reject("schema", "discard invalid DST game event")
            return True
        if not secrets.compare_digest(event.nonce, self.nonce):
            self.reject("nonce", "discard DST game event with an invalid nonce")
            return True

        if isinstance(event, PlayerShardEnteredEvent):
            self.instrumentation.set_player_count(self.instrumentation.player_count + 1)
        elif isinstance(event, PlayerShardLeftEvent):
            self.instrumentation.set_player_count(self.instrumentation.player_count - 1)
        if isinstance(event, PlayerActionEvent):
            self.instrumentation.record_action(
                event.data.action_id,
                event.data.success,
            )
        try:
            self.queue.put_nowait(
                ObservedGameEvent(
                    record=event,
                    observed_timestamp_ns=observed_timestamp_ns,
                )
            )
        except asyncio.QueueFull:
            self.dropped += 1
            self.instrumentation.record_event(
                "dropped",
                event_name=event.event,
                reason="queue_full",
            )
        else:
            self.instrumentation.change_queue_size(1)
            self.instrumentation.record_event("accepted", event_name=event.event)
        return True

    def reject(self, reason: str, message: str) -> None:
        self.invalid += 1
        self.instrumentation.record_event("invalid", reason=reason)
        logger.warning(message)

    def close(self) -> None:
        if self.eof:
            return
        self.eof = True
        self.instrumentation.set_process_up(False)
        self.instrumentation.set_player_count(0)
        if self.queue.full():
            _ = self.queue.get_nowait()
            self.instrumentation.change_queue_size(-1)
            self.dropped += 1
            self.instrumentation.record_event("dropped", reason="stream_closed")
        self.queue.put_nowait(None)


__all__ = ["GameEventStream"]
