from __future__ import annotations

import asyncio
import secrets

from logbook import Logger
from pydantic import ValidationError
from ulid import ULID

from dst_server.events import GAME_EVENT_ADAPTER, GameEvent, ObservedGameEvent
from dst_server.events.player import (
    ActionEvent,
    ShardEnteredEvent,
    ShardLeftEvent,
)

from .recorder import Recorder

QUEUE_SIZE = 1024
MAX_LINE_BYTES = 64 * 1024
PREFIX = "DST_OTEL|"
PREFIX_BYTES = PREFIX.encode()
logger = Logger(__name__)


def _encode_line(line: str | bytes) -> bytes:
    return line if isinstance(line, bytes) else line.encode()


class EventStream:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.nonce = str(ULID())
        self.queue: asyncio.Queue[ObservedGameEvent | None] = asyncio.Queue(
            maxsize=QUEUE_SIZE
        )
        self.eof = False
        self.invalid = 0
        self.dropped = 0
        self._unwarned_rejections = {"encoding", "oversized", "schema", "nonce"}

    async def read(self) -> ObservedGameEvent | None:
        if self.eof and self.queue.empty():
            return None
        return await self.queue.get()

    def accept(self, line: str | bytes, observed_timestamp_ns: int) -> bool:
        encoded_line = _encode_line(line)
        marker = encoded_line.find(PREFIX_BYTES)
        if marker < 0:
            return False
        if self.eof:
            return True

        encoded = encoded_line[marker:]
        if len(encoded) > MAX_LINE_BYTES:
            self.reject("oversized", "discard oversized DST game event")
            return True
        try:
            payload = encoded[len(PREFIX_BYTES) :].decode()
        except UnicodeDecodeError:
            self.reject("encoding", "discard non-UTF-8 DST game event")
            return True
        try:
            event = GAME_EVENT_ADAPTER.validate_json(payload, strict=True)
        except ValidationError:
            self.reject("schema", "discard invalid DST game event")
            return True
        if not secrets.compare_digest(event.nonce, self.nonce):
            self.reject("nonce", "discard DST game event with an invalid nonce")
            return True

        self._publish(event, observed_timestamp_ns)
        return True

    def _publish(self, event: GameEvent, observed_timestamp_ns: int) -> None:
        if isinstance(event, ShardEnteredEvent):
            self.recorder.set_player_count(self.recorder.player_count + 1)
        elif isinstance(event, ShardLeftEvent):
            self.recorder.set_player_count(self.recorder.player_count - 1)
        if isinstance(event, ActionEvent):
            self.recorder.record_action(
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
            self.recorder.record_event(
                "dropped",
                event_name=event.event,
                reason="queue_full",
            )
        else:
            self.recorder.record_event("accepted", event_name=event.event)

    def reject(self, reason: str, message: str) -> None:
        self.invalid += 1
        self.recorder.record_event("invalid", reason=reason)
        if reason in self._unwarned_rejections:
            self._unwarned_rejections.remove(reason)
            logger.warning(message)

    def close(self) -> None:
        if self.eof:
            return
        self.eof = True
        self.recorder.set_process_up(False)
        self.recorder.set_player_count(0)
        if self.queue.full():
            _ = self.queue.get_nowait()
            self.dropped += 1
            self.recorder.record_event("dropped", reason="stream_closed")
        self.queue.put_nowait(None)


__all__ = ["EventStream"]
