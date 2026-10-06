"""Game observations and batches delivered by an Agent subscription."""

from typing import Any, TypedDict, cast

lazy from ._native import event_names
lazy from ._native import validate_event as _validate_event

__all__ = ["GameEvent", "SubscriptionBatch", "event_names", "validate_event"]


class SubscriptionBatch(TypedDict, closed=True):
    """The final batch can contain records and report a closed stream."""

    records: list[Any]
    dropped: int
    closed: bool


class GameEvent(TypedDict, closed=True):
    """Gameplay observation emitted by a shard and validated by the SDK."""

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
    """Validate a Python value against the native game event schema.

    Unsupported Python types and non-string object keys raise TypeError.
    Nonfinite numbers and values over the JSON conversion limits raise ValueError.
    The native validator raises DstError when converted JSON violates the event schema.

    Returns:
        A validated copy of the game event.
    """
    return cast("GameEvent", _validate_event(value))
