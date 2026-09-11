import asyncio
import json

import pytest

from dst_server import commands as c
from dst_server.game.rpc import MAX_RESULT_LINE_BYTES
from dst_server.models.console import ConsoleResult
from tests.game.helpers import make_game
from tests.helpers import COMMAND_DONE, next_frame, run_lua
from tests.runtime.test_console import make_console


async def evaluate(source: str, runtime: str) -> ConsoleResult:
    game, commands = make_game()
    console, writer, reader = make_console()

    async def execute(command: str) -> str:
        commands.append(command)
        pending = asyncio.create_task(console.execute(command))
        try:
            _, _, wrapped = await next_frame(writer)
            output = run_lua(
                'require("dst_server.state").installed = true;'
                "local original_print = print;"
                + wrapped.decode()
                + "assert(print == original_print)",
                runtime,
            )
            reader.feed_data(output + COMMAND_DONE + b"\n")
            return await asyncio.wait_for(asyncio.shield(pending), 5)
        finally:
            pending.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(pending, return_exceptions=True)

    game.execute_ready = execute
    try:
        result = await game.invoke(c.Evaluate(source=source))
    finally:
        async with asyncio.timeout(5):
            await console.close()
    assert len(commands) == 1
    assert "\n" not in commands[0]
    return result


@pytest.mark.parametrize(
    ("source", "output", "values"),
    [
        ("1 + 2", "", [("number", "3")]),
        (
            'local x = 2\nprint("hello", x)\nreturn x, nil, false',
            "hello\t2",
            [
                ("number", "2"),
                ("nil", "nil"),
                ("boolean", "false"),
            ],
        ),
        ('print("中文😀")', "中文😀", []),
        ("local x = 2", "", []),
        ('return "a\\0b", math.huge', "", [("string", "a\0b"), ("number", "inf")]),
        ("return string.char(255)", "", [("string", r"\xff")]),
    ],
)
async def test_console_evaluates_expressions_and_multiline_once(
    source: str,
    output: str,
    values: list[tuple[str, str]],
    lua_runtime: str,
) -> None:
    result = await evaluate(source, lua_runtime)
    assert result.output == output
    assert [(item.type, item.text) for item in result.values] == values
    assert result.error is None
    assert not result.truncated


@pytest.mark.parametrize(
    ("source", "kind"),
    [("local =", "compile"), ('error("failure")', "runtime")],
)
async def test_console_distinguishes_compile_and_runtime_errors(
    source: str, kind: str, lua_runtime: str
) -> None:
    result = await evaluate(source, lua_runtime)
    assert result.error is not None
    assert result.error.kind == kind
    assert result.error.message
    assert result.values == ()


async def test_runtime_error_does_not_retry_the_statement(lua_runtime: str) -> None:
    result = await evaluate(
        '(function() print("executed"); error("failure") end)()', lua_runtime
    )
    assert result.output == "executed"
    assert result.error is not None
    assert result.error.kind == "runtime"


async def test_console_represents_arbitrary_objects_and_truncates(
    lua_runtime: str,
) -> None:
    result = await evaluate(
        "return {}, function() end, coroutine.create(function() end), "
        'setmetatable({}, {__tostring=function() error("failed") end}), '
        'string.rep("中", 1000)',
        lua_runtime,
    )
    assert [item.type for item in result.values] == [
        "table",
        "function",
        "thread",
        "table",
        "string",
    ]
    assert all(item.text for item in result.values)
    assert result.values[3].text == "<table: tostring failed>"
    assert result.values[4].text == "中" * 170
    assert result.truncated


async def test_console_bounds_values_in_worst_case_json(lua_runtime: str) -> None:
    result = await evaluate(
        "print(string.rep(string.char(0), 10000)); "
        "local values = {}; for i = 1, 100 do "
        "values[i] = string.rep(string.char(0), 10000) end; return unpack(values)",
        lua_runtime,
    )
    assert len(result.values) == 16
    assert all(len(item.text) == 512 for item in result.values)
    assert result.output == "\0" * 2048
    assert result.truncated
    assert len(json.dumps(result.model_dump(mode="json"))) < MAX_RESULT_LINE_BYTES


@pytest.mark.parametrize(
    "source",
    [
        'for i = 1, 8192 do print("x") end; return 42',
        'print(string.rep("x", 65536)); return 42',
        (
            "return setmetatable({}, {__tostring = function() "
            'print(string.rep("x", 65536)); return "42" end})'
        ),
    ],
    ids=["many-lines", "large-line", "value-rendering"],
)
async def test_console_bounds_print_output_and_preserves_values(
    source: str, lua_runtime: str
) -> None:
    result = await evaluate(source, lua_runtime)
    assert result.output.startswith("x")
    assert len(result.output.encode()) <= 2048
    assert result.truncated
    assert result.error is None
    assert [value.text for value in result.values] == ["42"]


def test_cached_console_print_forwards_after_evaluation(lua_runtime: str) -> None:
    output = run_lua(
        """
        local console = require("dst_server.console")
        local original_print = print
        for _, ending in ipairs({ "return 42", "error('failure')" }) do
            local result = console.evaluate({ source =
                'rawset(_G, "saved_print", print); print("captured"); ' .. ending })
            assert(result.output == "captured")
            assert(print == original_print)
            saved_print("after-evaluation")
            assert(result.output == "captured")
            local weak = setmetatable({ result }, { __mode = "v" })
            result = nil
            collectgarbage("collect")
            assert(weak[1] == nil)
        end
        """,
        lua_runtime,
    )
    assert output == b"after-evaluation\nafter-evaluation\n"
