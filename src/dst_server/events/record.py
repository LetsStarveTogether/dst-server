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
    AppearanceRequestedEvent,
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
    EmoteRequestedEvent,
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
    RescueRequestedEvent,
    RevivedEvent,
    ShardEnteredEvent,
    ShardLeftEvent,
    SkillChangedEvent,
    SpawnedEvent,
    UnequippedEvent,
)
from .vote import VoteClosedEvent, VoteResultEvent, VoteSubmittedEvent, VoteUpdatedEvent
from .world import (
    EntityDeathEvent,
    MapDeliveryStartedEvent,
    ModOutdatedEvent,
    PauseChangedEvent,
    RiftChangedEvent,
    RiftUnlockedEvent,
    ShardBossDefeatedEvent,
    ShardConnectionChangedEvent,
    StateChangedEvent,
    TelemetryErrorEvent,
    VaultTrialGuardsDefeatedEvent,
    VaultTrialProgressEvent,
)

type GameEvent = Annotated[
    ShardEnteredEvent
    | ClientAuthenticatedEvent
    | ClientDisconnectedEvent
    | PresenceEvent
    | ChatEvent
    | EmoteRequestedEvent
    | RescueRequestedEvent
    | AppearanceRequestedEvent
    | AnnouncementEvent
    | DiceRolledEvent
    | SkinReceivedEvent
    | SystemMessageEvent
    | VoteUpdatedEvent
    | VoteSubmittedEvent
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
    | MapDeliveryStartedEvent
    | VaultTrialProgressEvent
    | VaultTrialGuardsDefeatedEvent
    | TelemetryErrorEvent
    | ModOutdatedEvent,
    Field(discriminator="event"),
]

GAME_EVENT_ADAPTER = TypeAdapter(GameEvent)


@dataclass(frozen=True, slots=True)
class ObservedGameEvent:
    record: GameEvent
    observed_timestamp_ns: int
