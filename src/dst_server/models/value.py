from __future__ import annotations

from ..schema import FiniteFloat, FrozenModel, Identifier, PositiveInt


class Position(FrozenModel):
    x: FiniteFloat
    y: FiniteFloat
    z: FiniteFloat


class Entity(FrozenModel):
    prefab: Identifier
    guid: PositiveInt


__all__ = ["Entity", "Position"]
