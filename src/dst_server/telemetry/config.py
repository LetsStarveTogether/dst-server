from dst_server.models.base import FrozenModel, Identifier
from dst_server.models.telemetry import TelemetryProfile

DEFAULT_ACTIONS = (
    "ACTIVATE",
    "ADDFUEL",
    "ATTACK",
    "BUILD",
    "CASTAOE",
    "CASTSPELL",
    "CHOP",
    "CONSTRUCT",
    "COOK",
    "DEPLOY",
    "DIG",
    "EXTINGUISH",
    "FERTILIZE",
    "FISH",
    "FISH_OCEAN",
    "GIVE",
    "GIVETOPLAYER",
    "HAMMER",
    "HARVEST",
    "HEAL",
    "LIGHT",
    "MIGRATE",
    "MINE",
    "MURDER",
    "PICK",
    "PICKUP",
    "PLANT",
    "REPAIR",
    "REVIVE_CORPSE",
    "TELEPORT",
    "UNLOCK",
    "UPGRADE",
)


class TelemetrySettings(FrozenModel):
    profile: TelemetryProfile = "critical"
    actions: tuple[Identifier, ...] = DEFAULT_ACTIONS
