from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Percent = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type PercentagePoints = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
type NonNegativeInt = Annotated[int, Field(ge=0)]
type PositiveInt = Annotated[int, Field(gt=0)]
type Identifier = Annotated[str, Field(min_length=1, max_length=128)]
type Name = Annotated[str, Field(max_length=256)]
type Description = Annotated[str, Field(max_length=2048)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
    )


__all__ = [
    "Description",
    "FiniteFloat",
    "FrozenModel",
    "Identifier",
    "Name",
    "NonNegativeFloat",
    "NonNegativeInt",
    "Percent",
    "PercentagePoints",
    "PositiveInt",
]
