"""The room's latest observed activity, persisted by the host."""

from pydantic import AwareDatetime

from dst_server.configuration.overrides import FrozenMapping
from dst_server.models.base import FrozenModel


class ActivityCheckpoint(FrozenModel):
    sessions: FrozenMapping[str, str]
    last_active_at: AwareDatetime
