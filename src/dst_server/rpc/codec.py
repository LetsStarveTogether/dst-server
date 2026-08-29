import json
import math
from collections.abc import Mapping
from ipaddress import IPv4Address
from types import NoneType, UnionType
from typing import Annotated, Any, TypeAliasType, Union, cast, get_args, get_origin

from pydantic import BaseModel, JsonValue, SecretStr, TypeAdapter
from ulid import ULID

from dst_server.cluster.config import ClusterConfig
from dst_server.cluster.overrides import (
    CustomWorldOverrides,
    FrozenMapping,
    _LoadedCaveOverrides,
    _LoadedForestOverrides,
)
from dst_server.cluster.world import (
    CaveOverrides,
    ForestOverrides,
    LavaArenaOverrides,
    QuagmireOverrides,
    WorldOverrides,
)
from dst_server.events.server import SavedEvent
from dst_server.game.rpc import DriverHealth
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World

from .models import (
    ClusterStatus,
    GameEventRecord,
    LifecycleRecord,
    LocatedPlayer,
    LogRecord,
    ShardRuntimeStatus,
)

_WORLD_TYPES: dict[str, type[WorldOverrides]] = {
    "base": WorldOverrides,
    "forest": ForestOverrides,
    "cave": CaveOverrides,
    "quagmire": QuagmireOverrides,
    "lavaArena": LavaArenaOverrides,
    "custom": CustomWorldOverrides,
    "loadedForest": _LoadedForestOverrides,
    "loadedCave": _LoadedCaveOverrides,
}
_WORLD_TAGS = {model: name for name, model in _WORLD_TYPES.items()}
_WIRE_FORMAT_VERSION = 1
_PAYLOAD_MODELS: tuple[type[BaseModel], ...] = (
    ClusterConfig,
    ClusterStatus,
    ShardRuntimeStatus,
    LocatedPlayer,
    Player,
    Inventory,
    DriverHealth,
    Room,
    World,
    Runtime,
    Mod,
    ShardStatus,
    SavedEvent,
    LogRecord,
    LifecycleRecord,
    GameEventRecord,
)
_JSON = TypeAdapter(JsonValue)


def _annotation(annotation: object) -> object:
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        else:
            return annotation


def _json_value(value: object) -> object:  # ruff: ignore[complex-structure]
    if isinstance(value, WorldOverrides):
        try:
            kind = _WORLD_TAGS[type(value)]
        except KeyError:
            msg = f"unsupported RPC world override type: {type(value).__name__}"
            raise TypeError(msg) from None
        return [kind, _model_value(value)]
    if isinstance(value, BaseModel):
        return _model_value(value)
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, IPv4Address | ULID):
        return str(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            msg = "RPC JSON object keys must be strings"
            raise TypeError(msg)
        mapping = cast(Mapping[str, object], value)
        return {key: _json_value(mapping[key]) for key in sorted(mapping)}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, frozenset):
        return [_json_value(item) for item in sorted(value, key=repr)]
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        msg = "non-finite floats cannot cross RPC"
        raise ValueError(msg)
    msg = f"unsupported RPC JSON value: {type(value).__name__}"
    raise TypeError(msg)


def _model_value(value: BaseModel) -> dict[str, object]:
    return {
        name: _json_value(getattr(value, name))
        for name in sorted(value.model_fields_set)
    }


def _dump(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _payload_contract() -> bytes:
    models = {
        f"{model.__module__}.{model.__qualname__}": model.model_json_schema()
        for model in _PAYLOAD_MODELS
    }
    worlds = {tag: model.model_json_schema() for tag, model in _WORLD_TYPES.items()}
    return json.dumps(
        {
            "models": models,
            "wireFormat": _WIRE_FORMAT_VERSION,
            "worldOverrides": worlds,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"duplicate RPC JSON object key: {key!r}"
            raise ValueError(msg)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    msg = f"invalid RPC JSON constant: {value}"
    raise ValueError(msg)


def _load(value: Any) -> object:
    return json.loads(
        bytes(value),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        msg = "RPC JSON value must be a string"
        raise TypeError(msg)
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        msg = "RPC JSON value must be an array"
        raise TypeError(msg)
    return cast(list[object], value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = "RPC JSON value must be an object"
        raise TypeError(msg)
    return cast(dict[str, object], value)


def _restore_model[ModelT: BaseModel](model: type[ModelT], value: object) -> ModelT:
    if not isinstance(value, dict):
        return model.model_validate(value)
    fields = model.model_fields
    restored = {
        name: _restore(fields[name].annotation, item) if name in fields else item
        for name, item in value.items()
    }
    return model.model_validate(restored)


def _restore_world(annotation: type[WorldOverrides], value: object) -> WorldOverrides:
    match value:
        case [str() as kind, fields]:
            pass
        case _:
            msg = "invalid RPC world override value"
            raise ValueError(msg)
    try:
        model = _WORLD_TYPES[kind]
    except KeyError:
        msg = f"unknown RPC world override type: {kind}"
        raise ValueError(msg) from None
    if annotation is not WorldOverrides and model is not annotation:
        msg = f"RPC world override is not {annotation.__name__}"
        raise ValueError(msg)
    return _restore_model(model, fields)


def _restore(annotation: object, value: object) -> object:  # ruff: ignore[complex-structure]
    original = annotation
    annotation = _annotation(annotation)
    if isinstance(annotation, type) and issubclass(annotation, WorldOverrides):
        return _restore_world(annotation, value)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _restore_model(annotation, value)
    if annotation is SecretStr:
        return SecretStr(_text(value))
    if annotation is IPv4Address:
        return IPv4Address(_text(value))
    if annotation is ULID:
        return ULID.from_str(_text(value))

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {UnionType, Union}:
        if value is None and NoneType in arguments:
            return None
        candidates = tuple(item for item in arguments if item is not NoneType)
        if len(candidates) == 1:
            return _restore(candidates[0], value)
        return TypeAdapter(original).validate_json(_dump(value))
    if origin is tuple:
        item = arguments[0] if arguments else object
        return tuple(_restore(item, entry) for entry in _array(value))
    if origin is frozenset:
        item = arguments[0] if arguments else object
        return frozenset(_restore(item, entry) for entry in _array(value))
    if origin is list:
        item = arguments[0] if arguments else object
        return [_restore(item, entry) for entry in _array(value)]
    if origin is Mapping or origin is FrozenMapping:
        key_type, value_type = arguments
        return {
            _restore(key_type, key): _restore(value_type, item)
            for key, item in _object(value).items()
        }
    return value


def encode_model(value: BaseModel) -> bytes:
    return _dump(value)


def decode_model[ModelT: BaseModel](model: type[ModelT], value: object) -> ModelT:
    restored = _restore(model, _load(value))
    if not isinstance(restored, model):
        msg = f"RPC payload is not {model.__name__}"
        raise TypeError(msg)
    return restored


def encode_json_value(value: JsonValue) -> bytes:
    return _dump(value)


def decode_json_value(value: object) -> JsonValue:
    return _JSON.validate_python(_load(value), strict=True)
