from __future__ import annotations

import os
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario",
    ["off", "active", "partial_failure", "core_failure"],
)
def test_lua_driver(scenario: str) -> None:
    luajit = shutil.which("luajit")
    if luajit is None and os.environ.get("CI"):
        pytest.fail("LuaJIT is required in CI")
    if luajit is None:
        pytest.skip("LuaJIT is not installed")

    root = Path(__file__).parents[1]
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [
            luajit,
            str(root / "tests/lua/driver_spec.lua"),
            str(root / "src/dst_server/lua"),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout == "ok\n"
