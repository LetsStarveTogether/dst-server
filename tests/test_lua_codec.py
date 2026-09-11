import subprocess  # ruff: ignore[suspicious-subprocess-import]

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dst_server.lua_codec import (
    LuaValue,
    literal_calls,
    lua_string,
    lua_value,
    parse_literal,
    parse_return_table,
    render_literal,
)


@pytest.mark.parametrize(
    "value",
    ["", 'quote"\\', "\0\n\r\t", "玩家\U000f0001", "a\u200db\u2028c"],
)
def test_lua_string_round_trip_preserves_utf8(value: str) -> None:
    assert parse_literal(lua_string(value)) == value


@pytest.mark.parametrize(
    "value",
    [False, True, 0, -(2**53 - 1), 1.25, {"values": [1, False, "玩家"]}],
)
def test_configuration_literals_round_trip(value: LuaValue) -> None:
    assert parse_literal(render_literal(value)) == value


@pytest.mark.parametrize(
    "source",
    [
        'require("untrusted")',
        '{ ["same"]=1, ["same"]=2 }',
        '{ [1]="one", [3]="three" }',
        '{ [1]="one", named="two" }',
        "[[long string]]",
        "{value=string.char(10)}",
        r'"\256"',
        r'"\255"',
        r'"\128"',
        r'"\x41"',
        r'"\q"',
        '"value"; return "injected"',
    ],
)
def test_configuration_literals_reject_executable_or_ambiguous_data(
    source: str,
) -> None:
    with pytest.raises(
        ValueError, match=r"literal|string|duplicate|array|mixed|syntax errors"
    ):
        parse_literal(source)


def test_literal_tables_and_calls_share_the_same_static_language() -> None:
    assert parse_return_table('KLEI     1 return {name="房间"}', "index") == {
        "name": "房间"
    }
    assert parse_return_table("-- empty", "mods", allow_empty=True) == {}
    assert tuple(
        literal_calls("ServerModSetup('42'); return EnableModError()", "setup")
    ) == (("ServerModSetup", ("42",)), ("EnableModError", ()))
    assert tuple(literal_calls("return", "setup")) == ()


def test_rpc_values_keep_json_null_and_empty_object_semantics() -> None:
    assert lua_value(None) == 'require("json").null'
    assert lua_value({}) == 'require("dst_server.wire").object({})'
    assert lua_value([]) == "{}"
    with pytest.raises(ValueError, match="IEEE 754"):
        lua_value(2**53)


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=128))
def test_lua_string_property_round_trip(value: str) -> None:
    assert parse_literal(lua_string(value)) == value


def test_native_quoted_tables_match_lua_string_bytes(lua_runtime: str) -> None:
    source = (
        r"""return {
        ['single'] = '\a\b\f\n\r\t\v\\\'\"',
        ["double"] = "\a\b\f\n\r\t\v\\\'\"",
        ['decimal'] = '\231\142\169\229\174\182\000\12\0499',
        """
        "newline = 'left\\\nright', crlf = 'a\\\r\nb', lfcr = 'a\\\n\rb'}"
    )
    parsed = parse_return_table(source, "native configuration")
    program = (
        "local values = (function()\n" + source + "\nend)()\n"
        "for _, name in ipairs({'single', 'double', 'decimal', "
        "'newline', 'crlf', 'lfcr'}) "
        "do io.write(values[name]) end"
    )

    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [lua_runtime, "-"],
        input=program.encode(),
        capture_output=True,
        check=True,
        timeout=5,
    )

    expected = {
        "single": "\a\b\f\n\r\t\v\\'\"",
        "double": "\a\b\f\n\r\t\v\\'\"",
        "decimal": "玩家\0\x0c19",
        "newline": "left\nright",
        "crlf": "a\nb",
        "lfcr": "a\nb",
    }
    assert parsed == expected
    assert result.stdout == "".join(expected.values()).encode()
