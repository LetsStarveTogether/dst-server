from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_serializer,
)
from ulid import ULID

type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Percent = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type PercentagePoints = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
type NonNegativeInt = Annotated[int, Field(ge=0)]
type PositiveInt = Annotated[int, Field(gt=0)]
type Identifier = Annotated[str, Field(min_length=1, max_length=128)]
type Name = Annotated[str, Field(max_length=256)]
type Description = Annotated[str, Field(max_length=2048)]
type ULIDValue = Annotated[
    ULID, PlainSerializer(str, return_type=str, when_used="json")
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    # No return annotation: preserve each field's native serialization schema.
    @field_serializer("*", mode="wrap")
    def _serialize_field(  # ruff: ignore[missing-return-type-private-function]
        self,
        value: object,
        handler: SerializerFunctionWrapHandler,
        info: SerializationInfo,
    ):
        if (
            isinstance(value, SecretStr)
            and isinstance(info.context, dict)
            and info.context.get("secrets") is True
        ):
            return value.get_secret_value()
        return handler(value)

    def replace(self, **changes: object) -> Self:
        values = {field: getattr(self, field) for field in self.model_fields_set}
        values.update(changes)
        return type(self).model_validate(values)


class RevalidatedFrozenModel(FrozenModel):
    model_config = ConfigDict(revalidate_instances="always")
