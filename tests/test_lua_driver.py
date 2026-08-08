from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario",
    [
        "off",
        "active",
        "empty_actions",
        "critical",
        "telemetry_error_utf8",
        "wrapper_failures",
        "loot_limit",
        "partial_failure",
        "core_failure",
    ],
)
def test_lua_driver(scenario: str, luajit: str) -> None:
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
