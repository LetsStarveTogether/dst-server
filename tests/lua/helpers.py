import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

from luaparser import ast
from luaparser.astnodes import Function, LocalFunction, Name

from dst_server.lua_codec import lua_string


def run_lua_process(
    runtime: str, *arguments: str | Path, input: bytes | None = None
) -> bytes:
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [runtime, *map(str, arguments)],
        input=input,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, (result.stderr or result.stdout).decode(
        errors="replace"
    )
    return result.stdout


def native_scripts() -> Path:
    return Path(
        os.environ.get(
            "DST_SERVER_TEST_SCRIPTS", Path(__file__).parents[2] / "dst-scripts/scripts"
        )
    )


def native_functions(filename: str, names: set[str]) -> str:
    tree = ast.parse((native_scripts() / filename).read_text())
    functions = [
        (node.name.id, node)
        for node in tree.body.body
        if isinstance(node, (Function, LocalFunction))
        and isinstance(node.name, Name)
        and node.name.id in names
    ]
    assert {name for name, _ in functions} == names
    assert len(functions) == len(names)
    return "\n".join(ast.to_lua_source(node) for _, node in functions)


def run_lua(source: str, luajit: str, *, driver_path: bool = True) -> bytes:
    root = Path(__file__).parents[2]
    scripts = native_scripts()
    assert (scripts / "json.lua").is_file(), "Real DST json.lua is required"
    package_path = f"{scripts}/?.lua;"
    if driver_path:
        package_path = f"{root}/src/dst_server/lua/?.lua;" + package_path
    setup = (
        f'package.path={lua_string(package_path)}..package.path;json=require("json");'
    )
    return run_lua_process(luajit, "-", input=(setup + source).encode())
