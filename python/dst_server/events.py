"""Lossless game event values validated against the native producer contract."""

from typing import Any, Literal, TypedDict, cast

from ._native import event_schema
from ._native import validate_event as _validate_event

__all__ = ["GameEvent", "event_schema", "validate_event"]


class GameEvent(TypedDict):
    """Version 3 envelope; event_schema() describes every event payload."""

    v: Literal[3]
    nonce: str
    generation: int
    session_id: str | None
    seq: int
    event: str
    tick: int
    monotonic_ms: int
    cycle: int | None
    data: dict[str, Any]


def validate_event(value: object) -> GameEvent:
    """Return a validated copy, raising DstError for invalid native events."""
    return cast("GameEvent", _validate_event(value))
