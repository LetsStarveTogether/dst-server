from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, TypeAdapter

from .connection import (
    ClientAuthenticatedEvent,
    ClientDisconnectedEvent,
    PresenceEvent,
)
from .messages import (
    AnnouncementEvent,
    DiceRolledEvent,
    SkinReceivedEvent,
    SystemMessageEvent,
)
from .player import (
    ActionEvent,
    AteEvent,
    ChatEvent,
    CombatBlockedEvent,
    CombatHitEvent,
    CombatReceivedEvent,
    ConditionChangedEvent,
    CraftedEvent,
    DeployedEvent,
    DisconnectedEvent,
    DroppedEvent,
    EquippedEvent,
    FinishedWorkEvent,
    FishedEvent,
    GhostedEvent,
    HarvestedEvent,
    HoundWarningEvent,
    IncidentEvent,
    MigrationStartedEvent,
    PickedEvent,
    PlantedEvent,
    PlayerLoadedEvent,
    RevivedEvent,
    ShardEnteredEvent,
    ShardLeftEvent,
    SkillChangedEvent,
    SpawnedEvent,
    UnequippedEvent,
)
from .vote import VoteCastEvent, VoteClosedEvent, VoteResultEvent, VoteStartedEvent
from .world import (
    EntityDeathEvent,
    ModOutdatedEvent,
    PauseChangedEvent,
    RiftChangedEvent,
    RiftUnlockedEvent,
    ShardBossDefeatedEvent,
    ShardConnectionChangedEvent,
    StateChangedEvent,
    TelemetryErrorEvent,
)

type GameEvent = Annotated[
    ShardEnteredEvent
    | ClientAuthenticatedEvent
    | ClientDisconnectedEvent
    | PresenceEvent
    | ChatEvent
    | AnnouncementEvent
    | DiceRolledEvent
    | SkinReceivedEvent
    | SystemMessageEvent
    | VoteStartedEvent
    | VoteCastEvent
    | VoteClosedEvent
    | VoteResultEvent
    | PauseChangedEvent
    | PlayerLoadedEvent
    | ShardLeftEvent
    | DisconnectedEvent
    | MigrationStartedEvent
    | SpawnedEvent
    | GhostedEvent
    | RevivedEvent
    | EntityDeathEvent
    | ActionEvent
    | CombatHitEvent
    | CombatReceivedEvent
    | CombatBlockedEvent
    | StateChangedEvent
    | ShardBossDefeatedEvent
    | ShardConnectionChangedEvent
    | CraftedEvent
    | AteEvent
    | PickedEvent
    | HarvestedEvent
    | FinishedWorkEvent
    | DeployedEvent
    | EquippedEvent
    | UnequippedEvent
    | DroppedEvent
    | ConditionChangedEvent
    | IncidentEvent
    | FishedEvent
    | PlantedEvent
    | SkillChangedEvent
    | HoundWarningEvent
    | RiftUnlockedEvent
    | RiftChangedEvent
    | TelemetryErrorEvent
    | ModOutdatedEvent,
    Field(discriminator="event"),
]

GAME_EVENT_ADAPTER = TypeAdapter(GameEvent)


@dataclass(frozen=True, slots=True)
class ObservedGameEvent:
    record: GameEvent
    observed_timestamp_ns: int
