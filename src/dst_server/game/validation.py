from __future__ import annotations

import math


def required_string(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    return value


def player_id(value: str) -> str:
    return required_string("player userid", value)


def percent(name: str, value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        msg = f"{name} must be a finite number between 0 and 1"
        raise ValueError(msg)
    return float(value)


def number(name: str, value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        msg = f"{name} must be a finite number"
        raise ValueError(msg)
    return float(value)


def positive_timeout(value: float, name: str = "completion") -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        msg = f"{name} timeout must be positive"
        raise ValueError(msg)
    return float(value)


def item_count(value: int, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        msg = f"count must be a {qualifier} integer"
        raise ValueError(msg)
    return value


def prefab(value: str) -> str:
    return required_string("prefab", value).lower()
