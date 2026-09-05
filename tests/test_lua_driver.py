import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER


@pytest.mark.parametrize(
    "scenario",
    [
        "off",
        "active",
        "empty_actions",
        "critical",
        "finite_positions",
        "large_loot",
        "native_action_scope",
        "nested_action_scope",
        "native_action_failures",
        "action_truthiness",
        "action_traceback",
        "combat_causality",
        "wrapper_results",
        "diagnostics",
        "capture_failure",
        "encoding_failure",
        "diagnostic_failure",
        "oversized_event",
        "print_partial_failure",
        "print_reentrancy",
        "partial_failure",
        "invalid_options",
    ],
)
def test_lua_driver(scenario: str, luajit: str) -> None:
    root = Path(__file__).parents[1]
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [
            luajit,
            str(root / "tests/lua/driver_spec.lua"),
            str(root),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    *lines, status = result.stdout.splitlines()
    assert status == "ok"
    for line in lines:
        assert line.startswith("DST_OTEL|")
        record = GAME_EVENT_ADAPTER.validate_json(line.removeprefix("DST_OTEL|"))
        assert record.v == 2
        assert record.generation == 7
