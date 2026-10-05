import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SPECS = sorted(Path(__file__).parent.glob("*_spec.lua"))


@pytest.mark.parametrize("script", SPECS, ids=lambda path: path.stem)
def test_client_contracts(script: Path, lua_runtime: str, native_scripts: Path) -> None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [lua_runtime, str(script)],
        cwd=ROOT,
        env=os.environ | {"FIELDKIT_GAME_SCRIPTS": str(native_scripts)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_client_lua_syntax(lua_runtime: str) -> None:
    files = sorted(
        path
        for folder in (ROOT / "mods/fieldkit", Path(__file__).parent)
        for path in folder.rglob("*.lua")
    )
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            lua_runtime,
            "-",
            *map(str, files),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input="for i=1,#arg do assert(loadfile(arg[i])) end\n",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
