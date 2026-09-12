import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import JsonValue, ValidationError
from ulid import ULID

from dst_server.commands import validate_json_structure
from dst_server.events import GAME_EVENT_ADAPTER, GameEvent, ObservedGameEvent
from dst_server.events.connection import (
    ClientAuthenticatedEvent,
    ClientDisconnectedEvent,
    PresenceEvent,
)
from dst_server.events.player import (
    ActionEvent,
    PlayerLoadedEvent,
    ShardEnteredEvent,
    ShardLeftEvent,
)
from dst_server.events.world import ModOutdatedEvent
from dst_server.models.driver import DRIVER_RECORD_ADAPTER, DriverFailed, DriverRecord
from dst_server.models.telemetry import TelemetryProfile

from .recorder import Recorder

QUEUE_SIZE = 1024
MAX_LINE_BYTES = 64 * 1024
PREFIX = "DST_OTEL|"
LINE_PREFIX = re.compile(
    rb"(?:\[[0-9]{2,}:[0-5][0-9]:[0-5][0-9]\]: )?(DST_OTEL|DST_DRIVER)\|"
)


class EventStream:
    def __init__(
        self,
        recorder: Recorder,
        observe_driver: Callable[[DriverRecord], Awaitable[None]] | None = None,
        *,
        observe_event: Callable[[GameEvent], None] | None = None,
        profile: TelemetryProfile = "critical",
    ) -> None:
        self.observe_driver = observe_driver
        self.observe_event = observe_event
        self.profile = profile
        self.recorder = recorder
        self.nonce = str(ULID())
        self.outdated_mods: set[str] = set()
        self.queue: asyncio.Queue[ObservedGameEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.invalid = 0
        self.dropped = 0
        self.duplicates = 0
        self.stale = 0
        self.gaps = 0
        self.generation: int | None = None
        self.sequence = 0
        self.last_event_timestamp_ns: int | None = None
        self.last_presence_timestamp_ns: int | None = None
        self.last_active_at: datetime | None = None
        self._players: dict[int, str] = {}
        self._clients: set[str] = set()
        self._closed = False

    def start_generation(self, generation: int) -> None:
        if self.generation is not None and generation <= self.generation:
            return
        self.generation = generation
        self.sequence = 0
        self.last_event_timestamp_ns = None
        self.last_presence_timestamp_ns = None
        self.last_active_at = None
        self._players.clear()
        self._clients.clear()
        self.recorder.set_player_count(0)
        self.recorder.set_client_count(0)

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
        if len(marker.group(1)) + 1 + len(encoded) > MAX_LINE_BYTES:
            self.reject("oversized", observed_timestamp_ns)
            return True
        try:
            payload = encoded.decode()
        except UnicodeDecodeError:
            self.reject("encoding", observed_timestamp_ns)
            return True
        try:
            validate_json_structure(encoded)
        except ValueError:
            self.reject("schema", observed_timestamp_ns)
            return True
        if marker.group(1) == b"DST_DRIVER":
            await self._accept_driver(payload, observed_timestamp_ns)
            return True
        try:
            event = GAME_EVENT_ADAPTER.validate_json(payload, strict=True)
        except ValidationError as error:
            self.reject("schema", observed_timestamp_ns, error)
            return True
        if not secrets.compare_digest(event.nonce, self.nonce):
            self.reject("nonce", observed_timestamp_ns)
            return True

        self._publish(event, observed_timestamp_ns)
        return True

    async def _accept_driver(self, payload: str, observed_timestamp_ns: int) -> None:
        try:
            record = DRIVER_RECORD_ADAPTER.validate_json(payload, strict=True)
        except ValidationError as error:
            self.reject("schema", observed_timestamp_ns, error)
            return
        if record.nonce is None:
            self.reject("nonce", observed_timestamp_ns)
            if isinstance(record, DriverFailed) and self.observe_driver is not None:
                await self.observe_driver(record)
            return
        if not secrets.compare_digest(record.nonce, self.nonce):
            self.reject("nonce", observed_timestamp_ns)
            return
        if self.observe_driver is not None:
            await self.observe_driver(record)

    def _publish(self, event: GameEvent, observed_timestamp_ns: int) -> None:
        if self._closed:
            self._drop(event, "stream_closed", observed_timestamp_ns)
            return
        if self.generation is not None and event.generation < self.generation:
            self.stale += 1
            self.recorder.record_event("ignored", reason="stale_generation")
            return
        self.start_generation(event.generation)
        if event.seq <= self.sequence:
            if event.seq == self.sequence:
                self.duplicates += 1
                reason = "duplicate"
            else:
                self.stale += 1
                reason = "stale_sequence"
            self.recorder.record_event("ignored", reason=reason)
            return
        missing = event.seq - self.sequence - 1
        if missing:
            self.recorder.diagnostic(
                "sequence_gap",
                "sequence",
                observed_timestamp_ns,
                body={
                    "last_after": self.sequence,
                    "last_next": event.seq,
                    "last_generation": event.generation,
                },
                attributes={"dst.game.attempt.id": self.nonce},
                count=missing,
            )
            self.gaps += missing
            self.recorder.record_event("gap", reason="sequence", count=missing)
        self.sequence = event.seq
        self.last_event_timestamp_ns = observed_timestamp_ns
        observed = ObservedGameEvent(
            record=event, observed_timestamp_ns=observed_timestamp_ns
        )
        self._observe(observed)
        if self.profile != "off":
            self.recorder.observe_game(observed)
        if self.observe_event is not None:
            self.observe_event(event)
        if self.profile == "off":
            return
        self.recorder.record_event("accepted", event_name=event.event)
        if self.queue.full():
            self._drop(
                self.queue.get_nowait().record, "queue_full", observed_timestamp_ns
            )
        self.queue.put_nowait(observed)

    def _observe(self, observed: ObservedGameEvent) -> None:
        event = observed.record
        was_occupied = bool(self._players or self._clients)
        if isinstance(event, ModOutdatedEvent):
            self.outdated_mods.add(event.data.name)
        elif isinstance(event, PresenceEvent):
            self._players = {
                player.guid: player.userid for player in event.data.players
            }
            self._clients = set(event.data.clients)
            self.last_presence_timestamp_ns = observed.observed_timestamp_ns
        elif isinstance(event, ShardEnteredEvent | PlayerLoadedEvent):
            if event.data.player.userid is not None:
                self._players[event.data.player.guid] = event.data.player.userid
        elif isinstance(event, ShardLeftEvent):
            self._players.pop(event.data.player.guid, None)
        elif isinstance(event, ClientAuthenticatedEvent):
            self._clients.add(event.data.userid)
        elif isinstance(event, ClientDisconnectedEvent):
            self._clients.discard(event.data.userid)
            self._players = {
                guid: userid
                for guid, userid in self._players.items()
                if userid != event.data.userid
            }
        if (
            was_occupied
            or self._players
            or self._clients
            or isinstance(
                event, (PlayerLoadedEvent, ShardEnteredEvent, ClientAuthenticatedEvent)
            )
        ):
            self.last_active_at = datetime.fromtimestamp(
                observed.observed_timestamp_ns / 1_000_000_000, UTC
            )
        self.recorder.set_player_count(len(set(self._players.values())))
        self.recorder.set_client_count(len(self._clients))
        if isinstance(event, ActionEvent):
            self.recorder.record_action(event.data.action_id, event.data.success)

    def _drop(self, event: GameEvent, reason: str, observed_timestamp_ns: int) -> None:
        self.dropped += 1
        self.recorder.record_event("dropped", event_name=event.event, reason=reason)
        self.recorder.diagnostic(
            "notification_dropped",
            reason,
            observed_timestamp_ns,
            body={
                "last_event_name": event.event,
                "last_sequence": event.seq,
                "last_generation": event.generation,
            },
            attributes={"dst.game.attempt.id": self.nonce},
        )

    def reject(
        self,
        reason: str,
        observed_timestamp_ns: int,
        error: ValidationError | None = None,
    ) -> None:
        self.invalid += 1
        self.recorder.record_event("invalid", reason=reason)
        body: dict[str, JsonValue] = {}
        if error is not None:
            # Error inputs, messages and discriminator values can contain player
            # text or unauthenticated data. Keep only bounded schema error codes.
            body["error_types"] = sorted({
                item["type"]
                for item in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            })[:8]
        self.recorder.diagnostic(
            "rejected",
            reason,
            observed_timestamp_ns,
            body=body,
            attributes={"dst.game.attempt.id": self.nonce},
        )

    def close(self) -> None:
        self._closed = True
        self.queue.shutdown(immediate=False)
        self.recorder.flush_diagnostics()
        self.recorder.set_process_up(False)
        self.recorder.set_player_count(0)
        self.recorder.set_client_count(0)
