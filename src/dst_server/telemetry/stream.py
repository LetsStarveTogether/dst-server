import asyncio
import re
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
LINE_PREFIX = re.compile(
    rb"(?:\[[0-9]{2,}:[0-5][0-9]:[0-5][0-9]\]: )?" + re.escape(PREFIX.encode())
)
logger = Logger(__name__)


class EventStream:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.nonce = str(ULID())
        self.queue: asyncio.Queue[ObservedGameEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.invalid = 0
        self.dropped = 0
        self._unwarned_rejections = {"encoding", "oversized", "schema", "nonce"}

    async def read(self) -> ObservedGameEvent | None:
        try:
            return await self.queue.get()
        except asyncio.QueueShutDown:
            return None

    async def accept(self, line: str | bytes, observed_timestamp_ns: int) -> bool:
        encoded_line = (
            line if isinstance(line, bytes) else line.encode(errors="surrogatepass")
        )
        marker = LINE_PREFIX.match(encoded_line)
        if marker is None:
            return False

        encoded = encoded_line[marker.end() :]
        if len(PREFIX) + len(encoded) > MAX_LINE_BYTES:
            self.reject("oversized", "discard oversized DST game event")
            return True
        try:
            payload = encoded.decode()
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

        await self._publish(event, observed_timestamp_ns)
        return True

    async def _publish(self, event: GameEvent, observed_timestamp_ns: int) -> None:
        try:
            await self.queue.put(
                ObservedGameEvent(
                    record=event,
                    observed_timestamp_ns=observed_timestamp_ns,
                )
            )
        except asyncio.QueueShutDown:
            self.dropped += 1
            self.recorder.record_event(
                "dropped",
                event_name=event.event,
                reason="stream_closed",
            )
            return
        except asyncio.CancelledError:
            self.dropped += 1
            self.recorder.record_event(
                "dropped",
                event_name=event.event,
                reason="ingress_cancelled",
            )
            raise
        if isinstance(event, ShardEnteredEvent):
            self.recorder.set_player_count(self.recorder.player_count + 1)
        elif isinstance(event, ShardLeftEvent):
            self.recorder.set_player_count(self.recorder.player_count - 1)
        if isinstance(event, ActionEvent):
            self.recorder.record_action(event.data.action_id, event.data.success)
        self.recorder.record_event("accepted", event_name=event.event)

    def reject(self, reason: str, message: str) -> None:
        self.invalid += 1
        self.recorder.record_event("invalid", reason=reason)
        if reason in self._unwarned_rejections:
            self._unwarned_rejections.remove(reason)
            logger.warning(message)

    def close(self) -> None:
        self.queue.shutdown(immediate=False)
        self.recorder.set_process_up(False)
        self.recorder.set_player_count(0)
