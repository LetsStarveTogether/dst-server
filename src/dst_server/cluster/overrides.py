from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Literal, Never, Self, cast, get_args

from luaparser import ast
from luaparser.ast import SyntaxException
from luaparser.astnodes import (
    Call,
    FalseExpr,
    Name,
    Node,
    Number,
    Return,
    String,
    StringDelimiter,
    Table,
    TrueExpr,
    UMinusOp,
)
from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializeAsAny,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from dst_server.game.rpc import lua_string
from dst_server.models.base import RevalidatedFrozenModel

from .world import (
    CaveOverrides,
    CavePreset,
    ForestOverrides,
    ForestPreset,
    LavaArenaOverrides,
    QuagmireOverrides,
    WorldOverrides,
)


def _validate_lua_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "Lua strings must contain valid UTF-8"
        raise ValueError(msg) from error
    return value


type LuaString = Annotated[str, AfterValidator(_validate_lua_string)]
type LuaKey = LuaString
type NonEmptyLuaKey = Annotated[LuaString, Field(min_length=1)]
type Preset = Annotated[
    LuaString,
    Field(min_length=1, pattern=r"^[^\x00\r\n]*$"),
]

WORKSHOP_MOD = re.compile(r"workshop-([1-9][0-9]*)\Z")
LUA_NUMBER = re.compile(
    r"\s*[+-]?(?:nan|inf(?:inity)?|"
    r"0x(?:[0-9a-f]+(?:\.[0-9a-f]*)?|\.[0-9a-f]+)(?:p[+-]?[0-9]+)?|"
    r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)\s*\Z",
    re.IGNORECASE,
)
LUA_STRING_ESCAPE = re.compile(r'\\(?:(\r?\n)|([0-9]{1,3})|(["\\]))')
KLEI_FILE_HEADER = re.compile(r"\AKLEI[ \t]+1[ \t]+(?=return\b)")
MAX_SAFE_LUA_INTEGER = 2**53 - 1
MAX_WORKSHOP_ID = 2**64 - 1
MAX_MOD_NAME_BYTES = 255
MAX_LUA_BYTE = 255
FOREST_PRESETS = frozenset((*get_args(ForestPreset.__value__), "MOD_MISSING"))
CAVE_PRESETS = frozenset(get_args(CavePreset.__value__))
_BUILTIN_OVERRIDE_TYPES: dict[str, type[WorldOverrides]] = {
    **{preset.lower(): ForestOverrides for preset in FOREST_PRESETS},
    **{preset.lower(): CaveOverrides for preset in CAVE_PRESETS},
    "quagmire": QuagmireOverrides,
    "lavaarena": LavaArenaOverrides,
}


class CustomPreset(str):
    __slots__ = ()


class _FrozenDict[K, V](dict[K, V]):
    def _immutable(self, *_args: object, **_kwargs: object) -> Never:
        msg = "configuration containers are immutable"
        raise TypeError(msg)

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class _FrozenList[T](list[T]):
    def _immutable(self, *_args: object, **_kwargs: object) -> Never:
        msg = "configuration containers are immutable"
        raise TypeError(msg)

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _freeze_mapping[K, V](value: Mapping[K, V]) -> Mapping[K, V]:
    return _FrozenDict({
        key: cast(V, _deep_freeze(item)) for key, item in value.items()
    })


type FrozenMapping[K, V] = Annotated[
    Mapping[K, V],
    AfterValidator(_freeze_mapping),
]


type SafeLuaInteger = Annotated[
    int,
    Field(ge=-MAX_SAFE_LUA_INTEGER, le=MAX_SAFE_LUA_INTEGER),
]
type NonNegativeSafeLuaInteger = Annotated[
    int,
    Field(ge=0, le=MAX_SAFE_LUA_INTEGER),
]


def _reject_integer_float(value: object) -> object:
    if isinstance(value, int):
        msg = "Lua integers cannot be coerced to floats"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    return value


type LuaFloat = Annotated[
    float,
    BeforeValidator(_reject_integer_float),
    Field(allow_inf_nan=False),
]
type LuaScalar = LuaString | bool | SafeLuaInteger | LuaFloat
type LuaValue = LuaScalar | list[LuaValue] | dict[LuaKey, LuaValue]


def _validate_mod_name(name: str) -> str:
    if (
        name.casefold() == "client_mods_disabled"
        or len(name.encode("utf-8")) > MAX_MOD_NAME_BYTES
        or name in {".", ".."}
        or any(character in name for character in "\0/\\\r\n")
    ):
        msg = f"unsafe DST mod name: {name!r}"
        raise ValueError(msg)
    if name.startswith("workshop-"):
        match = WORKSHOP_MOD.fullmatch(name)
        if match is None or int(match.group(1)) > MAX_WORKSHOP_ID:
            msg = f"invalid DST Workshop mod name: {name!r}"
            raise ValueError(msg)
    return name


type ModName = Annotated[NonEmptyLuaKey, AfterValidator(_validate_mod_name)]


def _validate_force_enabled_mod_name(name: str) -> str:
    if LUA_NUMBER.fullmatch(name) and (
        not name.isascii()
        or not name.isdecimal()
        or name.startswith("0")
        or int(name) > MAX_WORKSHOP_ID
    ):
        msg = f"invalid numeric ForceEnableMod name: {name!r}"
        raise ValueError(msg)
    return name


type ForceEnabledModName = Annotated[
    ModName,
    AfterValidator(_validate_force_enabled_mod_name),
]
type WorkshopId = Annotated[int, Field(gt=0, le=MAX_WORKSHOP_ID)]


def _lua_literal(value: LuaValue, level: int = 0) -> str:
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        entries = (
            (f"[{lua_string(key)}]", item) for key, item in sorted(value.items())
        )
    elif isinstance(value, list):
        entries = ((f"[{index}]", item) for index, item in enumerate(value, 1))
    else:
        msg = f"unsupported Lua literal: {type(value).__name__}"
        raise TypeError(msg)

    indentation = "    " * (level + 1)
    body = "".join(
        f"{indentation}{key} = {_lua_literal(item, level + 1)},\n"
        for key, item in entries
    )
    if not body:
        return "{}"
    return f"{{\n{body}{'    ' * level}}}"


_WORLD_TABLE = TypeAdapter(dict[LuaKey, LuaScalar])


def _lua_statements(path: Path, description: str) -> list[Node]:
    if path.is_symlink():
        msg = f"{description} cannot be a symlink: {path}"
        raise ValueError(msg)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            chunk = ast.parse(KLEI_FILE_HEADER.sub("", stream.read(), count=1))
    except (OSError, SyntaxException, UnicodeError) as error:
        msg = f"invalid {description}: {path}: {error}"
        raise ValueError(msg) from error
    return list(chunk.body.body)


def _decode_lua_string(raw: str) -> str:
    value = bytearray()
    position = 0
    for match in LUA_STRING_ESCAPE.finditer(raw):
        literal = raw[position : match.start()]
        if "\\" in literal:
            msg = "unsupported Lua string escape"
            raise ValueError(msg)
        value.extend(literal.encode("utf-8"))
        if match.group(1) is not None:
            value.append(10)
        elif decimal := match.group(2):
            byte = int(decimal)
            if byte > MAX_LUA_BYTE:
                msg = "decimal Lua escape exceeds one byte"
                raise ValueError(msg)
            value.append(byte)
        else:
            value.extend(cast(str, match.group(3)).encode())
        position = match.end()
    literal = raw[position:]
    if "\\" in literal:
        msg = "unsupported Lua string escape"
        raise ValueError(msg)
    value.extend(literal.encode("utf-8"))
    return value.decode("utf-8")


def _lua_string_node(node: Node, description: str) -> str:
    if (
        not isinstance(node, String)
        or node.delimiter is not StringDelimiter.DOUBLE_QUOTE
    ):
        msg = f"{description} must be a canonical double-quoted string"
        raise ValueError(msg)
    try:
        return _decode_lua_string(node.raw)
    except (UnicodeError, ValueError) as error:
        msg = f"{description} is not a supported UTF-8 Lua string"
        raise ValueError(msg) from error


def _lua_key_node(node: Node | None, description: str) -> str:
    if isinstance(node, Name):
        return node.id
    return _lua_string_node(cast(Node, node), description)


def _lua_value_node(  # ruff: ignore[complex-structure, too-many-branches]
    node: Node,
    description: str,
) -> LuaValue:
    if isinstance(node, String):
        return _lua_string_node(node, description)
    if isinstance(node, TrueExpr | FalseExpr):
        return isinstance(node, TrueExpr)
    if isinstance(node, Number):
        return cast(int | float, node.n)
    if isinstance(node, UMinusOp) and isinstance(node.operand, Number):
        return -cast(int | float, node.operand.n)
    if not isinstance(node, Table):
        msg = f"{description} must contain only literal Lua values"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    if not node.fields:
        return {}

    numeric: dict[int, LuaValue] = {}
    mapping: dict[str, LuaValue] = {}
    implicit_index = 0
    for field in node.fields:
        key = field.key
        if key is None:
            if mapping:
                msg = f"{description} contains mixed table keys"
                raise ValueError(msg)
            implicit_index += 1
            if implicit_index in numeric:
                msg = f"{description} contains duplicate table keys"
                raise ValueError(msg)
            numeric[implicit_index] = _lua_value_node(field.value, description)
            continue
        if (
            isinstance(key, Number)
            and isinstance(key.n, int)
            and not isinstance(key.n, bool)
        ):
            if mapping or key.n in numeric:
                msg = f"{description} contains mixed or duplicate table keys"
                raise ValueError(msg)
            numeric[key.n] = _lua_value_node(field.value, description)
            continue
        if numeric:
            msg = f"{description} contains mixed table keys"
            raise ValueError(msg)
        if isinstance(key, Name) and field.between_brackets:
            msg = f"{description} table keys must be literal strings"
            raise ValueError(msg)
        name = _lua_key_node(key, f"{description} table key")
        if name in mapping:
            msg = f"{description} contains duplicate key {name!r}"
            raise ValueError(msg)
        mapping[name] = _lua_value_node(field.value, description)

    if numeric:
        if set(numeric) != set(range(1, len(numeric) + 1)):
            msg = f"{description} array keys must be consecutive from 1"
            raise ValueError(msg)
        return [numeric[index] for index in range(1, len(numeric) + 1)]
    return mapping


def _literal_return_table(
    path: Path,
    description: str,
    *,
    allow_empty: bool = False,
) -> dict[str, LuaValue]:
    statements = _lua_statements(path, description)
    if allow_empty and not statements:
        return {}
    if (
        len(statements) != 1
        or not isinstance(statements[0], Return)
        or len(statements[0].values) != 1
        or not isinstance(statements[0].values[0], Table)
    ):
        msg = f"{description} must contain one literal return table"
        raise ValueError(msg)
    value = _lua_value_node(statements[0].values[0], description)
    if not isinstance(value, dict):
        msg = f"{description} top-level value must be a table with string keys"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    return value


def _literal_call(
    statement: Node,
    description: str,
) -> tuple[str, list[Node]]:
    if not isinstance(statement, Call) or not isinstance(statement.func, Name):
        msg = f"{description} supports only direct literal function calls"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    return statement.func.id, list(statement.args)


def _literal_calls(path: Path, description: str) -> Iterator[tuple[str, list[Node]]]:
    statements = _lua_statements(path, description)
    for index, statement in enumerate(statements):
        call = statement
        if isinstance(statement, Return):
            if index != len(statements) - 1 or len(statement.values) > 1:
                msg = f"{description} permits only one final return value"
                raise ValueError(msg)
            if not statement.values:
                return
            call = statement.values[0]
        yield _literal_call(call, description)


class CustomWorldOverrides(WorldOverrides):
    __pydantic_extra__: dict[
        Annotated[str, AfterValidator(_validate_lua_string)],
        LuaScalar,
    ] = Field(init=False)
    model_config = ConfigDict(extra="allow", revalidate_instances="never")

    @model_validator(mode="after")
    def _freeze_values(self) -> Self:
        object.__setattr__(  # ruff: ignore[unnecessary-dunder-call]
            self,
            "__pydantic_extra__",
            _FrozenDict(self.__pydantic_extra__ or {}),
        )
        return self


class _LoadedForestOverrides(ForestOverrides, CustomWorldOverrides):
    islands: Literal["always", "never", "default", "sometimes", "often"] | None = None


class _LoadedCaveOverrides(CaveOverrides, CustomWorldOverrides):
    roads: Literal["never"] | None = None


_LOCATION_OVERRIDES: dict[str, type[WorldOverrides]] = {
    "forest": _LoadedForestOverrides,
    "cave": _LoadedCaveOverrides,
    "quagmire": QuagmireOverrides,
    "lavaarena": LavaArenaOverrides,
}


class LevelDataOverride(RevalidatedFrozenModel):
    id: Preset
    name: LuaString
    desc: LuaString
    location: Preset
    overrides: SerializeAsAny[WorldOverrides]
    background_node_range: tuple[SafeLuaInteger, SafeLuaInteger] | None = None
    required_prefabs: tuple[Preset, ...] | None = None
    version: Annotated[int, Field(ge=1, le=MAX_SAFE_LUA_INTEGER)] | None = None
    baseid: Preset | None = None
    blocker_blank_room_name: Preset | None = None
    custom_settings_desc: LuaString | None = None
    custom_settings_id: Preset | None = None
    custom_settings_name: LuaString | None = None
    custom_worldgen_desc: LuaString | None = None
    custom_worldgen_id: Preset | None = None
    custom_worldgen_name: LuaString | None = None
    hideminimap: bool | None = None
    hideinfrontend: bool | None = None
    max_playlist_position: SafeLuaInteger | None = None
    min_playlist_position: SafeLuaInteger | None = None
    numrandom_set_pieces: NonNegativeSafeLuaInteger | None = None
    ordered_story_setpieces: tuple[Preset, ...] | None = None
    override_level_string: bool | None = None
    override_triggers: FrozenMapping[LuaKey, LuaValue] | None = None
    playstyle: Preset | None = None
    random_set_pieces: tuple[Preset, ...] | None = None
    required_setpieces: tuple[Preset, ...] | None = None
    settings_desc: LuaString | None = None
    settings_baseid: Preset | None = None
    settings_id: Preset | None = None
    settings_name: LuaString | None = None
    substitutes: FrozenMapping[LuaKey, LuaValue] | None = None
    worldgen_desc: LuaString | None = None
    worldgen_baseid: Preset | None = None
    worldgen_id: Preset | None = None
    worldgen_name: LuaString | None = None

    @model_validator(mode="after")
    def _validate_location(self) -> Self:  # ruff: ignore[complex-structure, too-many-branches]
        task_set = self.overrides.model_dump(exclude_unset=True).get("task_set")
        if not isinstance(task_set, str) or not task_set:
            msg = "level data overrides require a non-empty task_set"
            raise ValueError(msg)
        if self.background_node_range is not None:
            minimum, maximum = self.background_node_range
            if minimum < 0 or maximum < minimum:
                msg = "background_node_range requires 0 <= minimum <= maximum"
                raise ValueError(msg)
        if self.numrandom_set_pieces and not self.random_set_pieces:
            msg = (
                "random_set_pieces cannot be empty when "
                "numrandom_set_pieces is positive"
            )
            raise ValueError(msg)
        if isinstance(self.overrides, QuagmireOverrides):
            expected = ("QUAGMIRE", "quagmire", "quagmire_portal")
        elif isinstance(self.overrides, LavaArenaOverrides):
            expected = ("LAVAARENA", "lavaarena", "lavaarena_portal")
        elif isinstance(self.overrides, ForestOverrides):
            if self.location != "forest":
                msg = "forest level data requires location='forest'"
                raise ValueError(msg)
            return self
        elif isinstance(self.overrides, CaveOverrides):
            if self.location != "cave":
                msg = "cave level data requires location='cave'"
                raise ValueError(msg)
            return self
        else:
            return self
        identifier, location, portal = expected
        if self.id != identifier or self.location != location:
            msg = f"{identifier} level data requires location={location!r}"
            raise ValueError(msg)
        if self.required_prefabs is not None and portal not in self.required_prefabs:
            msg = f"{identifier} level data requires prefab {portal!r}"
            raise ValueError(msg)
        if any(
            value is not None and value != identifier
            for value in (self.settings_id, self.worldgen_id)
        ):
            msg = "level data settings and worldgen IDs must match its ID"
            raise ValueError(msg)
        return self

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        overrides_type: type[WorldOverrides] | None = None,
    ) -> Self:
        values = _literal_return_table(path, "DST level data override configuration")
        if unknown := set(values).difference(cls.model_fields):
            msg = f"unknown level data override fields: {sorted(unknown)}"
            raise ValueError(msg)
        raw_overrides = values.get("overrides")
        if not isinstance(raw_overrides, dict):
            msg = "level data overrides must be a literal table with string keys"
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
        if overrides_type is None:
            location = values.get("location")
            overrides_type = (
                _LOCATION_OVERRIDES.get(location, CustomWorldOverrides)
                if isinstance(location, str)
                else CustomWorldOverrides
            )
        if not issubclass(overrides_type, WorldOverrides):
            msg = "overrides_type must derive from WorldOverrides"
            raise TypeError(msg)
        model_values: dict[str, object] = dict(values)
        model_values["overrides"] = overrides_type.model_validate(raw_overrides)
        for field in (
            "background_node_range",
            "ordered_story_setpieces",
            "random_set_pieces",
            "required_prefabs",
            "required_setpieces",
        ):
            value = model_values.get(field)
            if isinstance(value, list):
                model_values[field] = tuple(value)
            elif value == {}:
                model_values[field] = ()
        return cls.model_validate(model_values)

    def render(self) -> str:
        validated = type(self).model_validate(self)
        values = cast(
            dict[LuaKey, LuaValue],
            validated.model_dump(
                mode="json",
                by_alias=True,
                exclude={"overrides"},
                exclude_none=True,
                exclude_unset=True,
            ),
        )
        override_values = validated.overrides.model_dump(
            mode="python",
            by_alias=True,
            exclude_none=True,
            exclude_unset=True,
        )
        override_values = (
            type(validated.overrides)
            .model_validate(override_values)
            .model_dump(
                mode="python",
                by_alias=True,
                exclude_none=True,
                exclude_unset=True,
            )
        )
        values["overrides"] = cast(
            dict[LuaKey, LuaValue],
            _WORLD_TABLE.validate_python(override_values, strict=True),
        )
        return f"return {_lua_literal(values)}\n"


_WORLDGEN_FIELDS = frozenset({
    "override_enabled",
    "preset",
    "worldgen_preset",
    "settings_preset",
    "overrides",
})


def _upgrade_worldgen_v1(  # ruff: ignore[complex-structure]
    values: dict[str, LuaValue],
) -> dict[str, LuaValue]:
    if set(values).issubset(_WORLDGEN_FIELDS):
        return values
    raw_overrides = values.get("overrides", {})
    if not isinstance(raw_overrides, dict):
        msg = "world overrides must be a literal table with string keys"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    overrides = dict(raw_overrides)
    presetdata = values.get("presetdata")
    if presetdata is not None:
        if not isinstance(presetdata, dict):
            msg = "legacy world preset data must be a literal table"
            raise ValueError(msg)
        entries = presetdata.get("overrides", [])
        if entries == {}:
            entries = []
        if not isinstance(entries, list) or any(
            not isinstance(entry, list)
            or len(entry) != 2  # ruff: ignore[magic-value-comparison]
            or not isinstance(entry[0], str)
            for entry in entries
        ):
            msg = "legacy world preset overrides must contain key-value pairs"
            raise ValueError(msg)
        overrides.update(cast(list[tuple[str, LuaValue]], entries))
    for name, group in values.items():
        if name != "presetdata" and isinstance(group, dict):
            overrides.update(group)

    upgraded = {
        name: values[name]
        for name in ("worldgen_preset", "settings_preset", "override_enabled")
        if name in values
    }
    if "actualpreset" in values:
        upgraded["preset"] = values["actualpreset"]
    elif "preset" in values:
        upgraded["preset"] = values["preset"]
    upgraded["overrides"] = overrides
    return upgraded


class WorldgenOverride(RevalidatedFrozenModel):
    enabled: bool = True
    worldgen_preset: Preset | None = None
    settings_preset: Preset | None = None
    overrides: SerializeAsAny[WorldOverrides] = Field(default_factory=WorldOverrides)

    @model_validator(mode="after")
    def _validate_builtin_presets(self) -> Self:
        presets = (self.worldgen_preset, self.settings_preset)
        preset_types = {
            override_type
            for preset in presets
            if preset is not None
            and (override_type := _BUILTIN_OVERRIDE_TYPES.get(preset.lower()))
            is not None
        }
        if len(preset_types) > 1:
            msg = "different built-in world types cannot be combined"
            raise ValueError(msg)
        preset_type = next(iter(preset_types), None)
        override_type = next(
            (
                candidate
                for candidate in (
                    ForestOverrides,
                    CaveOverrides,
                    QuagmireOverrides,
                    LavaArenaOverrides,
                )
                if isinstance(self.overrides, candidate)
            ),
            None,
        )
        if preset_type in {QuagmireOverrides, LavaArenaOverrides} and (
            override_type is not preset_type
        ):
            msg = "event presets require their matching event overrides"
            raise ValueError(msg)
        if preset_type is not None and override_type not in {None, preset_type}:
            msg = "built-in presets and typed overrides must use the same world type"
            raise ValueError(msg)
        return self

    @classmethod
    def forest(
        cls,
        *,
        worldgen_preset: ForestPreset | CustomPreset = "SURVIVAL_TOGETHER",
        settings_preset: ForestPreset | CustomPreset = "SURVIVAL_TOGETHER",
        overrides: ForestOverrides | None = None,
    ) -> Self:
        if overrides is not None and not isinstance(overrides, ForestOverrides):
            msg = "forest worlds require ForestOverrides"
            raise TypeError(msg)
        if any(
            not isinstance(preset, CustomPreset)
            and (
                _BUILTIN_OVERRIDE_TYPES.get(preset.lower()) is not ForestOverrides
                or preset.lower() == "mod_missing"
            )
            for preset in (worldgen_preset, settings_preset)
        ):
            msg = "custom forest presets require CustomPreset"
            raise ValueError(msg)
        return cls(
            worldgen_preset=worldgen_preset,
            settings_preset=settings_preset,
            overrides=overrides or ForestOverrides(),
        )

    @classmethod
    def cave(
        cls,
        *,
        worldgen_preset: CavePreset | CustomPreset = "DST_CAVE",
        settings_preset: CavePreset | CustomPreset = "DST_CAVE",
        overrides: CaveOverrides | None = None,
    ) -> Self:
        if overrides is not None and not isinstance(overrides, CaveOverrides):
            msg = "cave worlds require CaveOverrides"
            raise TypeError(msg)
        if any(
            not isinstance(preset, CustomPreset)
            and _BUILTIN_OVERRIDE_TYPES.get(preset.lower()) is not CaveOverrides
            for preset in (worldgen_preset, settings_preset)
        ):
            msg = "custom cave presets require CustomPreset"
            raise ValueError(msg)
        return cls(
            worldgen_preset=worldgen_preset,
            settings_preset=settings_preset,
            overrides=overrides or CaveOverrides(),
        )

    @classmethod
    def load(  # ruff: ignore[complex-structure, too-many-branches]
        cls,
        path: Path,
        *,
        overrides_type: type[WorldOverrides] | None = None,
    ) -> Self:
        values = _upgrade_worldgen_v1(
            _literal_return_table(path, "DST world override configuration")
        )
        if (preset := values.pop("preset", None)) is not None:
            values.setdefault("worldgen_preset", preset)
            values.setdefault("settings_preset", preset)
        raw_overrides = values.get("overrides", {})
        if not isinstance(raw_overrides, dict):
            msg = "world overrides must be a literal table with string keys"
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]

        if overrides_type is None:
            presets = {
                value
                for field in ("worldgen_preset", "settings_preset")
                if isinstance((value := values.get(field)), str)
            }
            candidates: tuple[type[WorldOverrides], ...]
            builtin_types = {
                override_type
                for preset in presets
                if (override_type := _BUILTIN_OVERRIDE_TYPES.get(preset.lower()))
                is not None
            }
            if builtin_types:
                candidates = tuple(builtin_types)
            elif not raw_overrides:
                candidates = (WorldOverrides,)
            elif presets:
                candidates = (CustomWorldOverrides,)
            else:
                candidates = (ForestOverrides, CaveOverrides)
            validated_overrides = []
            for candidate in candidates:
                try:
                    validated_overrides.append(candidate.model_validate(raw_overrides))
                except ValidationError:
                    continue
            if len(validated_overrides) != 1:
                msg = (
                    "world type is ambiguous or custom; pass overrides_type="
                    "ForestOverrides, CaveOverrides, or a typed subclass"
                )
                raise ValueError(msg)
            overrides = validated_overrides[0]
        else:
            if not issubclass(overrides_type, WorldOverrides):
                msg = "overrides_type must derive from WorldOverrides"
                raise TypeError(msg)
            overrides = overrides_type.model_validate(raw_overrides)

        model_values: dict[str, object] = {"enabled": False, **values}
        if "override_enabled" in model_values:
            model_values["enabled"] = model_values.pop("override_enabled")
        model_values["overrides"] = overrides
        return cls.model_validate(model_values)

    def render(self) -> str:
        validated = type(self).model_validate(self)
        override_values = validated.overrides.model_dump(
            mode="python",
            by_alias=True,
            exclude_none=True,
            exclude_unset=True,
        )
        override_values = (
            type(validated.overrides)
            .model_validate(override_values)
            .model_dump(
                mode="python",
                by_alias=True,
                exclude_none=True,
                exclude_unset=True,
            )
        )
        overrides = _lua_literal(
            cast(
                LuaValue,
                _WORLD_TABLE.validate_python(
                    override_values,
                    strict=True,
                ),
            ),
            1,
        )
        lines = [
            "return {",
            f"    override_enabled = {str(validated.enabled).lower()},",
        ]
        if validated.worldgen_preset is not None:
            lines.append(
                f"    worldgen_preset = {lua_string(validated.worldgen_preset)},"
            )
        if validated.settings_preset is not None:
            lines.append(
                f"    settings_preset = {lua_string(validated.settings_preset)},"
            )
        lines.extend((f"    overrides = {overrides},", "}"))
        return "\n".join(lines) + "\n"


class ModOverride(RevalidatedFrozenModel):
    enabled: bool | None = None
    configuration_options: FrozenMapping[LuaKey, LuaValue] | None = None


class ModOverrides(RevalidatedFrozenModel):
    client_mods_disabled: bool | None = None
    entries: FrozenMapping[ModName, ModOverride] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Self:
        raw = _literal_return_table(
            path,
            "DST Mod override configuration",
            allow_empty=True,
        )
        values: dict[str, object] = {}
        if "client_mods_disabled" in raw:
            values["client_mods_disabled"] = raw.pop("client_mods_disabled")
        entries = {}
        for name, raw_entry in raw.items():
            if not isinstance(raw_entry, dict):
                msg = f"Mod override {name!r} must be a literal table"
                raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
            if unknown := set(raw_entry).difference({
                "enabled",
                "configuration_options",
            }):
                msg = f"unknown Mod override fields for {name!r}: {sorted(unknown)}"
                raise ValueError(msg)
            entry: dict[str, object] = {}
            if "enabled" in raw_entry:
                entry["enabled"] = raw_entry["enabled"]
            if "configuration_options" in raw_entry:
                options = raw_entry["configuration_options"]
                if not isinstance(options, dict):
                    msg = f"configuration_options for {name!r} must be a literal table"
                    raise ValueError(msg)
                entry["configuration_options"] = options
            entries[name] = ModOverride.model_validate(entry)
        values["entries"] = entries
        return cls.model_validate(values)

    @property
    def workshop_items(self) -> frozenset[int]:
        validated = type(self).model_validate(self)
        return frozenset(
            int(match.group(1))
            for name, override in validated.entries.items()
            if override.enabled is True
            and (match := WORKSHOP_MOD.fullmatch(name)) is not None
        )

    def render(self) -> str:
        validated = type(self).model_validate(self)
        values: dict[LuaKey, LuaValue] = {}
        if validated.client_mods_disabled is not None:
            values["client_mods_disabled"] = validated.client_mods_disabled
        for name, override in validated.entries.items():
            entry: dict[LuaKey, LuaValue] = {}
            if override.enabled is not None:
                entry["enabled"] = override.enabled
            if override.configuration_options is not None:
                options = dict(override.configuration_options)
                if options:
                    entry["configuration_options"] = options
            values[name] = entry
        return f"return {_lua_literal(values)}\n"


class ModSettings(RevalidatedFrozenModel):
    force_enabled: frozenset[ForceEnabledModName] = frozenset()
    debug_print: bool = False
    mod_errors: bool = False
    disable_mod_disabling: bool = False
    disable_local_mod_warning: bool = False

    @classmethod
    def load(cls, path: Path) -> Self:
        force_enabled = set()
        enabled = {
            "EnableModDebugPrint": "debug_print",
            "EnableModError": "mod_errors",
            "DisableModDisabling": "disable_mod_disabling",
            "DisableLocalModWarning": "disable_local_mod_warning",
        }
        values: dict[str, object] = {}
        for function, arguments in _literal_calls(
            path,
            "DST Mod settings configuration",
        ):
            if function == "ForceEnableMod":
                if len(arguments) != 1:
                    msg = "ForceEnableMod requires one literal string"
                    raise ValueError(msg)
                force_enabled.add(
                    _lua_string_node(arguments[0], "ForceEnableMod argument")
                )
            elif (field := enabled.get(function)) is not None:
                if arguments:
                    msg = f"{function} does not accept arguments"
                    raise ValueError(msg)
                values[field] = True
            else:
                msg = f"unsupported Mod settings function: {function}"
                raise ValueError(msg)
        if force_enabled:
            values["force_enabled"] = frozenset(force_enabled)
        return cls.model_validate(values)

    @property
    def workshop_items(self) -> frozenset[int]:
        validated = type(self).model_validate(self)
        items = set()
        for name in validated.force_enabled:
            if match := WORKSHOP_MOD.fullmatch(name):
                items.add(int(match.group(1)))
            elif name.isascii() and name.isdecimal():
                items.add(int(name))
        return frozenset(items)

    def render(self) -> str:
        validated = type(self).model_validate(self)
        lines = [
            f"ForceEnableMod({lua_string(name)})"
            for name in sorted(validated.force_enabled)
        ]
        for enabled, function in (
            (validated.debug_print, "EnableModDebugPrint"),
            (validated.mod_errors, "EnableModError"),
            (validated.disable_mod_disabling, "DisableModDisabling"),
            (validated.disable_local_mod_warning, "DisableLocalModWarning"),
        ):
            if enabled:
                lines.append(f"{function}()")
        return "".join(f"{line}\n" for line in lines)


class WorkshopDownloads(RevalidatedFrozenModel):
    items: frozenset[WorkshopId] = frozenset()
    collections: frozenset[WorkshopId] = frozenset()

    @classmethod
    def load(cls, path: Path) -> Self:
        values = {
            "ServerModSetup": set(),
            "ServerModCollectionSetup": set(),
        }
        for function, arguments in _literal_calls(
            path,
            "DST Workshop download configuration",
        ):
            if function not in values or len(arguments) != 1:
                msg = "Workshop setup supports one literal ID per setup call"
                raise ValueError(msg)
            identifier = _lua_string_node(
                arguments[0],
                f"{function} argument",
            )
            if not identifier and function == "ServerModSetup":
                continue
            if (
                not identifier.isascii()
                or not identifier.isdecimal()
                or identifier.startswith("0")
            ):
                msg = f"invalid Workshop ID: {identifier!r}"
                raise ValueError(msg)
            values[function].add(int(identifier))
        return cls(
            items=frozenset(values["ServerModSetup"]),
            collections=frozenset(values["ServerModCollectionSetup"]),
        )

    def render(self) -> str:
        validated = type(self).model_validate(self)
        return "".join(
            [*(f'ServerModSetup("{item}")\n' for item in sorted(validated.items))]
            + [
                f'ServerModCollectionSetup("{collection}")\n'
                for collection in sorted(validated.collections)
            ]
        )


__all__ = [
    "CustomPreset",
    "CustomWorldOverrides",
    "LevelDataOverride",
    "LuaValue",
    "ModOverride",
    "ModOverrides",
    "ModSettings",
    "WorkshopDownloads",
    "WorldgenOverride",
]
