from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from dst_server.lua_codec import NonNegativeSafeLuaInteger, PositiveSafeLuaInteger

from .base import FrozenModel


class DriverDiagnostic(FrozenModel):
    stage: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:]{0,127}$")]
    message: Literal[
        "callback_failed",
        "encoding_failed",
        "event_too_large",
        "installation_failed",
    ]
    count: PositiveSafeLuaInteger


class DriverHealth(FrozenModel):
    protocol: Literal[2]
    generation: NonNegativeSafeLuaInteger
    telemetry_status: Literal["disabled", "active", "degraded", "failed"]
    last_error: DriverDiagnostic | None
    events_emitted: NonNegativeSafeLuaInteger
    errors: NonNegativeSafeLuaInteger


type DriverNonce = Annotated[str, Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")]


class DriverStarting(FrozenModel):
    nonce: DriverNonce
    generation: NonNegativeSafeLuaInteger


class DriverReady(FrozenModel):
    nonce: DriverNonce
    health: DriverHealth


class DriverFailed(FrozenModel):
    nonce: DriverNonce | None
    generation: NonNegativeSafeLuaInteger | None
    error: Literal["configuration_failed", "installation_failed", "publication_failed"]


type DriverRecord = DriverStarting | DriverReady | DriverFailed
DRIVER_RECORD_ADAPTER = TypeAdapter(DriverRecord)
