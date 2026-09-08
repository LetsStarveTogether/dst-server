import json
import re
from collections.abc import Iterator
from typing import Annotated, cast

from luaparser import ast
from luaparser.ast import SyntaxException
from luaparser.astnodes import (
    Call,
    FalseExpr,
    Name,
    Node,
    Number,
    Return,
    SemiColon,
    String,
    StringDelimiter,
    Table,
    TrueExpr,
    UMinusOp,
)
from pydantic import AfterValidator, BeforeValidator, Field, JsonValue

MAX_SAFE_LUA_INTEGER = 2**53 - 1
MAX_LUA_BYTE = 255
LUA_STRING_ESCAPE = re.compile(r'\\(?:(\r?\n)|([0-9]{1,3})|(["\\]))')
KLEI_FILE_HEADER = re.compile(r"\AKLEI[ \t]+1[ \t]+(?=return\b)")


def validate_lua_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "Lua strings must contain valid UTF-8"
        raise ValueError(msg) from error
    return value


def _reject_integer_float(value: object) -> object:
    if isinstance(value, int):
        msg = "Lua integers cannot be coerced to floats"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    return value


type LuaString = Annotated[str, AfterValidator(validate_lua_string)]
type LuaKey = LuaString
type SafeLuaInteger = Annotated[
    int, Field(ge=-MAX_SAFE_LUA_INTEGER, le=MAX_SAFE_LUA_INTEGER)
]
type NonNegativeSafeLuaInteger = Annotated[int, Field(ge=0, le=MAX_SAFE_LUA_INTEGER)]
type LuaFloat = Annotated[
    float, BeforeValidator(_reject_integer_float), Field(allow_inf_nan=False)
]
type LuaScalar = LuaString | bool | SafeLuaInteger | LuaFloat
type LuaValue = LuaScalar | list[LuaValue] | dict[LuaKey, LuaValue]


def lua_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "Lua strings must contain valid UTF-8"
        raise ValueError(msg) from error
    escaped = []
    for character in value:
        if character == '"':
            escaped.append('\\"')
        elif character == "\\":
            escaped.append("\\\\")
        elif character.isprintable():
            escaped.append(character)
        else:
            escaped.extend(f"\\{byte:03d}" for byte in character.encode())
    return f'"{"".join(escaped)}"'


def lua_value(value: JsonValue) -> str:
    return _lua_value(value, set())


def _lua_value(value: JsonValue, seen: set[int]) -> str:
    if value is None:
        return 'require("json").null'
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, (bool, int, float)):
        if isinstance(value, int) and abs(value) > MAX_SAFE_LUA_INTEGER:
            msg = "Lua integers must be within the exact IEEE 754 integer range"
            raise ValueError(msg)
        return json.dumps(value, allow_nan=False)
    if not isinstance(value, (list, dict)):
        msg = "Lua values must be JSON values"
        raise TypeError(msg)
    identity = id(value)
    if identity in seen:
        msg = "Lua value contains a cycle"
        raise ValueError(msg)
    seen.add(identity)
    try:
        if isinstance(value, list):
            return "{" + ",".join(_lua_value(item, seen) for item in value) + "}"
        if any(not isinstance(key, str) for key in value):
            msg = "Lua object keys must be strings"
            raise TypeError(msg)
        if not value:
            return 'require("dst_server.wire").object({})'
        return (
            "{"
            + ",".join(
                f"[{lua_string(key)}]={_lua_value(item, seen)}"
                for key, item in value.items()
            )
            + "}"
        )
    finally:
        seen.remove(identity)


def render_literal(value: LuaValue, level: int = 0) -> str:
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
        f"{indentation}{key} = {render_literal(item, level + 1)},\n"
        for key, item in entries
    )
    if not body:
        return "{}"
    return f"{{\n{body}{'    ' * level}}}"


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
        return node.n
    if isinstance(node, UMinusOp) and isinstance(node.operand, Number):
        return -node.operand.n
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


def parse_literal(source: str, description: str = "Lua value") -> LuaValue:
    try:
        statements = list(ast.parse(f"return {source}").body.body)
    except (SyntaxException, UnicodeError) as error:
        msg = f"invalid {description}: {error}"
        raise ValueError(msg) from error
    if (
        len(statements) != 1
        or not isinstance(statements[0], Return)
        or len(statements[0].values) != 1
    ):
        msg = f"{description} must contain one literal Lua value"
        raise ValueError(msg)
    return _lua_value_node(statements[0].values[0], description)


def parse_return_table(
    source: str,
    description: str,
    *,
    allow_empty: bool = False,
) -> dict[str, LuaValue]:
    statements = parse_statements(source, description)
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


def parse_statements(source: str, description: str) -> list[Node]:
    try:
        chunk = ast.parse(KLEI_FILE_HEADER.sub("", source, count=1))
    except (SyntaxException, UnicodeError) as error:
        msg = f"invalid {description}: {error}"
        raise ValueError(msg) from error
    return [
        statement
        for statement in chunk.body.body
        if not isinstance(statement, SemiColon)
    ]


def literal_calls(
    source: str, description: str
) -> Iterator[tuple[str, tuple[LuaValue, ...]]]:
    statements = parse_statements(source, description)
    for index, statement in enumerate(statements):
        call = statement
        if isinstance(statement, Return):
            if index != len(statements) - 1 or len(statement.values) > 1:
                msg = f"{description} permits only one final return value"
                raise ValueError(msg)
            if not statement.values:
                return
            call = statement.values[0]
        name, arguments = _literal_call(call, description)
        yield (
            name,
            tuple(_lua_value_node(argument, description) for argument in arguments),
        )
