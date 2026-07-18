from __future__ import annotations

from enum import StrEnum
from typing import Any

from luaparser import ast


class LuaType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    TABLE = "table"
    NIL = "nil"
    FUNCTION = "function"
    USERDATA = "userdata"
    ANY = "any"


DEFAULT_VALUES = {
    LuaType.TABLE: "{}",
    LuaType.NUMBER: "0",
    LuaType.FUNCTION: "function()end",
    LuaType.USERDATA: "newproxy()",
    LuaType.STRING: '""',
    LuaType.NIL: "nil",
    LuaType.ANY: "variable",
}


def infer_type(node: Any) -> LuaType:
    match node:
        case ast.String() | ast.Concat():
            return LuaType.STRING
        case (
            ast.Number()
            | ast.AddOp()
            | ast.SubOp()
            | ast.MultOp()
            | ast.FloatDivOp()
            | ast.FloorDivOp()
        ):
            return LuaType.NUMBER
        case ast.TrueExpr() | ast.FalseExpr():
            return LuaType.BOOLEAN
        case ast.Table():
            return LuaType.TABLE
        case ast.Nil():
            return LuaType.NIL
        case ast.Function():
            return LuaType.FUNCTION
        case ast.Call(func=ast.Name(id="newproxy")):
            return LuaType.USERDATA
        case _:
            return LuaType.ANY


def construct_value(
    lua_type: LuaType | list[LuaType] | None,
    source: str | None = None,
) -> str:
    if lua_type is None:
        return "variable"

    if isinstance(lua_type, list):
        values = source.split(",") if source else []
        return ",".join(
            construct_value(
                item_type,
                values[index].strip() if index < len(values) else None,
            )
            for index, item_type in enumerate(lua_type)
        )

    if lua_type == LuaType.BOOLEAN:
        value = source.strip() if source else None
        return value if value in {"true", "false"} else "false"

    if source and source.strip() == "inst":
        return "inst"

    return DEFAULT_VALUES.get(lua_type, "variable")
