from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from dst_server.commands import validate_json_structure
from dst_server.errors import ErrorCode, ErrorInfo, IndeterminateError, RemoteError

JSON_VALUE = TypeAdapter(JsonValue, config=ConfigDict(allow_inf_nan=False, strict=True))
ERROR = TypeAdapter(ErrorInfo)


def encode[T](adapter: TypeAdapter[T], value: T) -> bytes:
    validated = adapter.validate_python(value, strict=True)
    return adapter.dump_json(
        validated,
        exclude_unset=True,
        context={"secrets": True},
        warnings="error",
    )


def decode[T](adapter: TypeAdapter[T], payload: bytes) -> T:
    validate_json_structure(payload)
    return adapter.validate_json(payload, strict=True)


def encode_model(value: BaseModel) -> bytes:
    return encode(TypeAdapter(type(value)), value)


def decode_model[T: BaseModel](model: type[T], payload: bytes) -> T:
    return decode(TypeAdapter(model), payload)


def encode_json_value(value: JsonValue) -> bytes:
    return encode(JSON_VALUE, value)


def decode_json_value(payload: bytes) -> JsonValue:
    return decode(JSON_VALUE, payload)


def unwrap_outcome(value: Any) -> Any:
    selected = value.which()
    if selected == "error":
        error = decode(ERROR, value.error)
        if error.code is ErrorCode.INDETERMINATE:
            raise IndeterminateError(error)
        raise RemoteError(error)
    if selected != "value":
        msg = f"invalid RPC outcome member: {selected}"
        raise ValueError(msg)
    return value.value


def success(value: object | None = None) -> dict[str, object]:
    return {"value": {} if value is None else value}


def failure(error: ErrorInfo) -> dict[str, object]:
    return {"error": encode(ERROR, error)}
