from typing import Literal

from .base import FrozenModel, NonNegativeInt

type TelemetryProfile = Literal["off", "critical", "history"]


class DeliveryStatus(FrozenModel):
    pending: NonNegativeInt = 0
    quarantined: NonNegativeInt = 0
    bytes: NonNegativeInt = 0
    last_error: str | None = None
