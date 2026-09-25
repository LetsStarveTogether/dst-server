from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.events.world import (
    MapDeliveryStartedEvent,
    VaultTrialGuardsDefeatedEvent,
    VaultTrialProgressEvent,
)
from tests.lua.helpers import run_lua_process


@pytest.mark.parametrize("profile", ["critical", "history", "off"])
def test_native_delivery_and_vault_events(
    native_scripts: Path, lua_runtime: str, profile: str
) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/gameplay_events_spec.lua",
        root,
        native_scripts,
        profile,
    )
    events = [
        GAME_EVENT_ADAPTER.validate_json(line, strict=True)
        for line in output.splitlines()
    ]
    if profile == "off":
        assert events == []
        return
    assert sum(isinstance(event, MapDeliveryStartedEvent) for event in events) == 2
    assert sum(isinstance(event, VaultTrialProgressEvent) for event in events) == 4
    bonuses = [
        event for event in events if isinstance(event, VaultTrialGuardsDefeatedEvent)
    ]
    assert len(bonuses) == 1
    assert bonuses[0].data.bonus_loot
    assert bonuses[0].data.trial.prefab == "vault_key_trial"
