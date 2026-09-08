from typing import Literal

from dst_server.events.base import DriverDiagnostic

from .base import FrozenModel, NonNegativeInt


class DriverHealth(FrozenModel):
    protocol: Literal[2]
    generation: NonNegativeInt
    telemetry_status: Literal["disabled", "active", "degraded", "failed"]
    last_error: DriverDiagnostic | None
    events_emitted: NonNegativeInt
    errors: NonNegativeInt
