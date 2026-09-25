"""The room's latest observed activity, persisted in its control file."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, Field

from dst_server.configuration.overrides import FrozenMapping
from dst_server.models.base import FrozenModel
from dst_server.models.driver import Presence


class ActivityCheckpoint(FrozenModel):
    sessions: FrozenMapping[str, str]
    last_active_at: AwareDatetime
    observations: FrozenMapping[str, str] = Field(default_factory=dict)
    clean_shutdown: bool = False


def observe(
    previous: ActivityCheckpoint | None,
    presence: Mapping[str, Presence],
    now: datetime,
    *,
    resume: bool = False,
) -> ActivityCheckpoint:
    sessions = {name: value.session_id for name, value in presence.items()}
    observations = {name: value.observation for name, value in presence.items()}
    continuous = (
        previous is not None
        and previous.sessions == sessions
        and (previous.observations == observations or resume)
    )
    last_active = previous.last_active_at if continuous and previous else now
    for value in presence.values():
        if value.client_count or value.player_count:
            last_active = now
        elif value.idle_seconds < value.observed_seconds:
            last_active = max(last_active, now - timedelta(seconds=value.idle_seconds))
    return ActivityCheckpoint(
        sessions=sessions,
        observations=observations,
        last_active_at=last_active.astimezone(UTC),
    )
