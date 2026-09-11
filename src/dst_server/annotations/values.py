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
