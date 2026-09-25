from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from tests.lua.helpers import run_lua_process


@pytest.mark.parametrize("mode", ["gorge", "forge", "forge_no", "forge_tie"])
def test_mode_votes_preserve_native_results_and_capture_submissions(
    native_scripts: Path, lua_runtime: str, mode: str
) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime, root / "tests/lua/mode_votes_spec.lua", root, native_scripts, mode
    )
    records = [GAME_EVENT_ADAPTER.validate_json(line) for line in output.splitlines()]
    assert sum(record.event == "dst.vote.submitted" for record in records) == (
        3 if mode == "gorge" else 2
    )
    assert not any(record.event == "dst.vote.result" for record in records)
    assert sum(record.event == "dst.vote.closed" for record in records) == (
        mode != "gorge"
    )
    assert all(
        record.data.source == ("gorge_voter" if mode == "gorge" else "lobbyvote")
        for record in records
    )
