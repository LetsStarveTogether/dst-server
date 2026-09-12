from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from tests.lua.helpers import run_lua_process


@pytest.mark.parametrize(
    "scenario",
    [
        "off",
        "mod_outdated_off",
        "mod_outdated_history",
        "mod_outdated_native_error",
        "mod_outdated_capture_error",
        "mod_outdated_telemetry_failed",
        "mod_outdated_missing_callback",
        "chat_wrapper",
        "chat_off",
        "chat_native_error",
        "chat_capture_error",
        "loaded_off",
        "loaded_critical",
        "loaded_history",
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
        "missing_clocks",
        "invalid_options",
    ],
)
def test_lua_driver(native_scripts: Path, scenario: str, lua_runtime: str) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime, root / "tests/lua/driver_spec.lua", root, scenario, native_scripts
    )
    *lines, status = output.decode().splitlines()
    assert status == "ok"
    for line in lines:
        assert line.startswith("DST_OTEL|")
        record = GAME_EVENT_ADAPTER.validate_json(line.removeprefix("DST_OTEL|"))
        assert record.v == 2
        assert record.generation == 7
